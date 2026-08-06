#!/usr/bin/env python3
"""Archie Intent Review — PR-time semantic review of the architectural source of truth.

Runs inside a GitHub Action on `pull_request`. It does NOT re-derive what changed —
the change is already folded into `.archie/blueprint.json` + `rules.json` on the branch
by `/archie-sync`. This script:

  1. Diffs the branch's blueprint/rules against the PR base ref (DETERMINISTIC — the
     script owns `diff_op`/ids/layer; the model never re-derives them).
  2. Globs the sync ledger (`.archie/changes/change_*.json`) for corroborating intent.
  3. Makes ONE Claude (Haiku) call to JUDGE the diff against the *retained* rules:
     is a change a silent weakening, a contradiction, or behavior that violates a rule?
  4. Posts ONE upserted FYI comment. It surfaces; the human decides. It NEVER blocks
     (always exits 0) and honors because-or-suppress (no cited rationale -> dropped).

Zero dependencies beyond the Python 3.9+ stdlib. Designed to run as
`python3 .archie/intent_review.py` with env: ANTHROPIC_API_KEY, GITHUB_TOKEN,
GITHUB_REPOSITORY, GITHUB_BASE_REF, GITHUB_EVENT_PATH.

Pure functions (diff/glob/parse/render) are importable and network-free so the test
suite can exercise them without hitting any API.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import IgnoreMatcher, file_sha1, is_source_path  # noqa: E402
import llm_client  # noqa: E402

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
MAX_TOKENS = 4096
COMMENT_MARKER = "<!-- archie-intent-review -->"
GITHUB_API = "https://api.github.com"

ADVISORY_KINDS = {"decision", "pitfall", "rule", "guideline"}
DESCRIPTIVE_KINDS = {"behavior", "structure", "dataflow", "data", "tech", "reference"}

# Blueprint sections we diff for Layer-1 silent-weakening, with their identity field.
# (field is None -> key on a hash of the title field instead.)
INVARIANT_SECTIONS = [
    # (top_key, sub_key_or_None, id_field, title_field)
    ("domain_invariants", None, "id", "invariant"),
    ("derived_invariants", None, "id", "invariant"),
    ("pitfalls", None, "id", "problem_statement"),
]
# `unenforced_invariants` are DELIBERATELY not diffed: they are documented GAPS
# (advisory, ungrounded), not standing law, so removing one is not a "weakening".
# (Design open question — see docs/archie-intent-review-design.md §13.)

# decisions.* sub-sections have no id -> title-hash keyed (Layer 1).
DECISION_SECTIONS = ["key_decisions", "trade_offs", "out_of_scope"]
DECISION_TITLE_FIELD = "title"

# Structured Layer-2 sections (keyed by name) we diff for behavior-violates-rule.
# `components` is included so a component REMOVE / responsibility change is caught
# cleanly (keyed, not noisy textual). NOT covered (deliberate POC scope): the purely
# descriptive snapshots — communication[], architecture_diagram, technology[],
# quick_reference[], implementation_guidelines[], data_overview. Those reflect current
# code (not prescriptive law) and a textual diff of them is the design's lower-precision
# path; behavior-level violations still surface via DESCRIPTIVE LEDGER CLAIMS. Textual
# fallback for those sections is a documented future enhancement.
DATA_SECTIONS = [
    ("components", "name"),
    ("data_models", "name"),
    ("persistence_stores", "name"),
]

# Rule sources diffed for contradiction / rule-removal (both keyed by id).
RULE_FILES = [".archie/rules.json", ".archie/platform_rules.json"]

RELEVANCE_SEND_ALL_THRESHOLD = 25   # if retained rules are few, skip the keyword filter
KEYWORD_JOIN_THRESHOLD = 1          # >=1 shared keyword token to attach ledger confidence


# ---------------------------------------------------------------------------
# git / file loading
# ---------------------------------------------------------------------------
def run_git(repo_root: Path, *args: str, timeout: int = 15):
    """Run git; return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # pragma: no cover - defensive
        return 1, "", str(e)


def _parse_json(text: str):
    """Parse JSON text; return (data, error). Empty/whitespace -> ({}, None)."""
    if text is None or not text.strip():
        return {}, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"


def fetch_base_file(repo_root: Path, base_ref: str, rel_path: str):
    """Read `rel_path` from the base ref via `git show`.

    Returns (exists: bool, data: dict|list|None, error: str|None) and CRITICALLY
    distinguishes two non-zero outcomes:
      - the file is genuinely ABSENT at a VALID ref (e.g. the first PR to add .archie/)
        -> (False, None, None): a legitimate all-ADD case.
      - the REF ITSELF is unresolvable (bad SHA / unknown revision) -> (False, None, err):
        the DANGEROUS case — the caller must NOT silently degrade to an empty baseline,
        or it would post a confident but wrong "everything is new" review.
    Malformed JSON at a valid ref -> (True, None, "<err>").
    """
    code, out, err = run_git(repo_root, "show", f"{base_ref}:{rel_path}")
    if code != 0:
        low = (err or "").lower()
        # File absent at a VALID ref — git says the path doesn't exist *in* the ref.
        if "does not exist in" in low or "exists on disk, but not in" in low:
            return False, None, None
        # Ref unresolvable or any other git failure — surface it, do not pretend absent.
        return False, None, err.strip() or "git show failed"
    data, perr = _parse_json(out)
    return True, data, perr


def load_branch_file(repo_root: Path, rel_path: str):
    """Read `rel_path` from the working tree (already checked out).

    Returns (exists, data, error) mirroring fetch_base_file.
    """
    p = repo_root / rel_path
    if not p.exists():
        return False, None, None
    try:
        data, perr = _parse_json(p.read_text())
        return True, data, perr
    except OSError as e:  # pragma: no cover - defensive
        return True, None, str(e)


# ---------------------------------------------------------------------------
# rules normalization
# ---------------------------------------------------------------------------
def normalize_rules(data) -> list:
    """rules.json may be {'rules': [...]}, a flat list, or absent. Always -> list."""
    if data is None:
        return []
    if isinstance(data, dict):
        rules = data.get("rules")
        return rules if isinstance(rules, list) else []
    if isinstance(data, list):
        return data
    return []


# ---------------------------------------------------------------------------
# keyed semantic diff
# ---------------------------------------------------------------------------
def _hash_title(title: str) -> str:
    return "title_" + hashlib.md5((title or "").strip().encode("utf-8")).hexdigest()[:8]


def item_key(item: dict, id_field: str, title_field: str) -> str:
    """Stable key for an item: its id if present, else a hash of its title, else a
    hash of the whole item — so title-less items (e.g. some trade_offs) do NOT all
    collide on the empty-string key.
    """
    if id_field and isinstance(item, dict):
        val = item.get(id_field)
        if val:
            return str(val)
    title = ""
    if isinstance(item, dict):
        title = str(item.get(title_field, "") or "")
    if title.strip():
        return _hash_title(title)
    try:
        blob = json.dumps(item, sort_keys=True, ensure_ascii=False)
        return "item_" + hashlib.md5(blob.encode("utf-8")).hexdigest()[:8]
    except (TypeError, ValueError):
        return _hash_title(title)


def _changed_fields(base_item: dict, branch_item: dict) -> list:
    keys = set()
    if isinstance(base_item, dict):
        keys |= set(base_item.keys())
    if isinstance(branch_item, dict):
        keys |= set(branch_item.keys())
    changed = []
    for k in sorted(keys):
        if (base_item or {}).get(k) != (branch_item or {}).get(k):
            changed.append(k)
    return changed


def keyed_diff(base_list, branch_list, id_field, title_field):
    """Return [{status, key, base_item, branch_item, fields_changed}].

    status in REMOVE | UPDATE | ADD. Reordered-but-identical lists -> no diffs.
    """
    base_list = base_list if isinstance(base_list, list) else []
    branch_list = branch_list if isinstance(branch_list, list) else []
    base_by = {}
    for it in base_list:
        if isinstance(it, dict):
            base_by[item_key(it, id_field, title_field)] = it
    branch_by = {}
    for it in branch_list:
        if isinstance(it, dict):
            branch_by[item_key(it, id_field, title_field)] = it

    out = []
    for key in base_by:
        if key not in branch_by:
            out.append({"status": "REMOVE", "key": key,
                        "base_item": base_by[key], "branch_item": None,
                        "fields_changed": []})
        else:
            fc = _changed_fields(base_by[key], branch_by[key])
            if fc:
                out.append({"status": "UPDATE", "key": key,
                            "base_item": base_by[key], "branch_item": branch_by[key],
                            "fields_changed": fc})
    for key in branch_by:
        if key not in base_by:
            out.append({"status": "ADD", "key": key,
                        "base_item": None, "branch_item": branch_by[key],
                        "fields_changed": []})
    return out


def _get_section(bp, top_key, sub_key):
    if not isinstance(bp, dict):
        return []
    node = bp.get(top_key)
    if sub_key:
        node = node.get(sub_key) if isinstance(node, dict) else None
    return node if isinstance(node, list) else []


def _title_of(item, title_field) -> str:
    if isinstance(item, dict):
        return str(item.get(title_field) or item.get("title") or item.get("name")
                   or item.get("invariant") or item.get("id") or "(unnamed)")
    return "(unnamed)"


# ---------------------------------------------------------------------------
# build the list of CHANGED ITEMS the model will judge
# ---------------------------------------------------------------------------
def build_changed_items(base_bp, branch_bp, base_rules, branch_rules, ledger_claims):
    """Deterministically assemble every reviewable change with a stable `ref`.

    Each item: {ref, source, section, diff_op, layer, title, base_item, branch_item,
                fields_changed, keywords, enforced_at_files}.
    The model references `ref`; the script owns diff_op/layer/section/title.
    """
    items = []
    n = [0]

    def add(source, section, diff_op, layer, title, base_item, branch_item,
            fields_changed, keywords, enforced_at_files):
        ref = f"c{n[0]}"
        n[0] += 1
        items.append({
            "ref": ref, "source": source, "section": section,
            "diff_op": diff_op, "layer": layer, "title": title,
            "base_item": base_item, "branch_item": branch_item,
            "fields_changed": fields_changed, "keywords": keywords,
            "enforced_at_files": enforced_at_files,
        })

    # Layer 1 — invariant sections (silent weakening)
    for top_key, sub_key, id_field, title_field in INVARIANT_SECTIONS:
        diffs = keyed_diff(_get_section(base_bp, top_key, sub_key),
                           _get_section(branch_bp, top_key, sub_key),
                           id_field, title_field)
        for d in diffs:
            ref_item = d["branch_item"] or d["base_item"] or {}
            add("blueprint", top_key, d["status"], 1,
                _title_of(ref_item, title_field),
                d["base_item"], d["branch_item"], d["fields_changed"],
                _keywords_of(ref_item), _enforced_files(ref_item))

    # Layer 1 — decisions.{key_decisions,trade_offs,out_of_scope} (title-hash keyed)
    for sub in DECISION_SECTIONS:
        dec_diffs = keyed_diff(_get_section(base_bp, "decisions", sub),
                               _get_section(branch_bp, "decisions", sub),
                               None, DECISION_TITLE_FIELD)
        for d in dec_diffs:
            ref_item = d["branch_item"] or d["base_item"] or {}
            add("blueprint", f"decisions.{sub}", d["status"], 1,
                _title_of(ref_item, DECISION_TITLE_FIELD),
                d["base_item"], d["branch_item"], d["fields_changed"],
                _keywords_of(ref_item), [])

    # Layer 1 — rules (contradiction candidates): ADD/UPDATE only
    rule_diffs = keyed_diff(base_rules, branch_rules, "id", "description")
    for d in rule_diffs:
        if d["status"] == "REMOVE":
            # a removed rule is a weakening of the ruleset
            ref_item = d["base_item"] or {}
            add("rules", "rules", "REMOVE", 1,
                _rule_title(ref_item), d["base_item"], None, [],
                _keywords_of(ref_item), [])
        else:
            ref_item = d["branch_item"] or {}
            add("rules", "rules", d["status"], 1,
                _rule_title(ref_item), d["base_item"], d["branch_item"],
                d["fields_changed"], _keywords_of(ref_item), [])

    # Layer 2 — data sections (behavior-violates-rule)
    for top_key, name_field in DATA_SECTIONS:
        diffs = keyed_diff(_get_section(base_bp, top_key, None),
                           _get_section(branch_bp, top_key, None),
                           name_field, name_field)
        for d in diffs:
            # Surface every data-section change (incl. pure ADDs). The script owns
            # WHAT changed; the model decides whether a new/changed model violates a
            # retained rule (it's told not to flag benign additions).
            ref_item = d["branch_item"] or d["base_item"] or {}
            add("blueprint", top_key, d["status"], 2,
                _title_of(ref_item, name_field),
                d["base_item"], d["branch_item"], d["fields_changed"],
                _keywords_of(ref_item), [])

    # Layer 2 — descriptive ledger claims (behavior-violates-rule)
    for claim in ledger_claims:
        if not isinstance(claim, dict):
            continue
        if claim.get("kind") in DESCRIPTIVE_KINDS:
            stmt = str(claim.get("statement", "")).strip()
            if not stmt:
                continue
            add("ledger", f"claim:{claim.get('kind')}", "DECLARED", 2,
                stmt[:80], None, claim, [],
                _keywords_from_text(stmt), list(claim.get("evidence_files") or []))

    return items


def _keywords_of(item) -> list:
    if not isinstance(item, dict):
        return []
    kw = item.get("keywords")
    if isinstance(kw, list):
        return [str(k).lower() for k in kw]
    return _keywords_from_text(" ".join(
        str(item.get(f, "")) for f in ("invariant", "title", "description", "name")))


def _keywords_from_text(text: str) -> list:
    toks = [t.strip(".,:;()[]'\"").lower() for t in (text or "").split()]
    return [t for t in toks if len(t) >= 4]


def _enforced_files(item) -> list:
    """File paths referenced by an invariant's enforced_at / evidence."""
    if not isinstance(item, dict):
        return []
    files = []
    for field in ("enforced_at", "evidence"):
        vals = item.get(field)
        if isinstance(vals, list):
            for v in vals:
                files.append(str(v).split(":")[0].strip())
    return [f for f in files if f]


def _rule_title(rule) -> str:
    if not isinstance(rule, dict):
        return "(rule)"
    return str(rule.get("id") or rule.get("topic") or rule.get("description", "")[:60] or "(rule)")


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------
def glob_ledger(repo_root: Path, base_ref: str) -> list:
    """Union of all claims from `.archie/changes/change_*.json` new on the branch.

    `latest.json` is overwritten on every record, so it is NOT a complete source — we
    glob the versioned files. Records already present on the base ref are skipped.
    Malformed records are skipped, not fatal. Claims deduped by id (or statement).
    """
    changes_dir = repo_root / ".archie" / "changes"
    if not changes_dir.is_dir():
        return []

    # which change files already exist on the base ref (so they aren't "new")
    base_files = set()
    code, out, _ = run_git(repo_root, "ls-tree", "-r", "--name-only", base_ref, ".archie/changes")
    if code == 0:
        base_files = {line.strip() for line in out.splitlines() if line.strip()}

    claims = []
    seen = set()
    for fp in sorted(changes_dir.glob("change_*.json")):
        rel = f".archie/changes/{fp.name}"
        if rel in base_files:
            continue  # already on base — not part of this PR's intent
        try:
            record = json.loads(fp.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for claim in (record.get("claims") or []):
            if not isinstance(claim, dict):
                continue
            key = claim.get("id") or claim.get("statement")
            if key in seen:
                continue
            seen.add(key)
            claims.append(claim)
    return claims


def ledger_join(changed_item: dict, claims: list):
    """Conservative join: attach a claim's confidence to an invariant change only when
    file paths overlap AND keyword overlap clears the threshold. No match -> None
    (the finding still surfaces, just without the confidence sharpener — never guess).
    """
    item_files = set(changed_item.get("enforced_at_files") or [])
    item_kw = set(changed_item.get("keywords") or [])
    best = None
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_files = set(str(f) for f in (claim.get("evidence_files") or []))
        file_overlap = bool(item_files & claim_files) or _path_overlap(item_files, claim_files)
        claim_kw = set(_keywords_from_text(str(claim.get("statement", ""))))
        kw_overlap = len(item_kw & claim_kw)
        if file_overlap and kw_overlap >= KEYWORD_JOIN_THRESHOLD:
            cand = {
                "confidence": claim.get("confidence"),
                "reconstructed": bool(claim.get("reconstructed", False)),
                "statement": claim.get("statement"),
            }
            if best is None or kw_overlap > best.get("_kw", 0):
                cand["_kw"] = kw_overlap
                best = cand
    if best:
        best.pop("_kw", None)
    return best


def _path_overlap(a: set, b: set) -> bool:
    for x in a:
        for y in b:
            if x and y and (x == y or x.endswith("/" + y) or y.endswith("/" + x)
                            or x in y or y in x):
                return True
    return False


# ---------------------------------------------------------------------------
# retained rules (context for the model)
# ---------------------------------------------------------------------------
def retained_rules(base_rules: list, changed_items: list) -> list:
    """Base-ref rules NOT themselves changed, optionally relevance-filtered."""
    changed_rule_keys = {
        it["title"] for it in changed_items if it.get("source") == "rules"
    }
    retained = [r for r in base_rules if isinstance(r, dict)
                and _rule_title(r) not in changed_rule_keys]
    if len(retained) <= RELEVANCE_SEND_ALL_THRESHOLD:
        return retained
    # relevance filter: keep rules sharing a keyword with any changed item
    changed_kw = set()
    for it in changed_items:
        changed_kw |= set(it.get("keywords") or [])
        changed_kw |= set(_keywords_from_text(it.get("title", "")))
    filtered = []
    for r in retained:
        rkw = set(_keywords_of(r)) | set(_keywords_from_text(str(r.get("description", ""))))
        if rkw & changed_kw:
            filtered.append(r)
    return filtered or retained[:RELEVANCE_SEND_ALL_THRESHOLD]


# ---------------------------------------------------------------------------
# model call
# ---------------------------------------------------------------------------
EMIT_FINDINGS_TOOL = {
    "name": "emit_findings",
    "description": (
        "Emit CONSOLIDATED review findings about a PR's change to the architectural "
        "source of truth. Emit ONE finding per DISTINCT change — NOT one per rule, and "
        "NOT one per code symbol. If the SAME underlying change appears across multiple "
        "functions/files/items, report it ONCE and list every item_ref it spans. In each "
        "finding, list ALL rules/invariants it collides with in `colliding_rules`, and "
        "write ONE consolidated, verifiable BECAUSE covering them. The diff op and which "
        "items changed are GIVEN (cite item_refs); you judge the TYPE and the BECAUSE. "
        "BECAUSE-OR-SUPPRESS: if you cannot ground it in the provided texts, omit it. "
        "Prefer FEW, well-consolidated findings over many repetitive ones."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_refs": {"type": "array", "items": {"type": "string"},
                                      "description": "ALL changed-item refs this one change spans (e.g. ['c0','c1']). A finding resolving to no listed item is discarded."},
                        "type": {"type": "string",
                                 "enum": ["silent_weakening", "contradiction", "behavior_violates_rule"]},
                        "change_summary": {"type": "string",
                                           "description": "short, specific title of the change, e.g. 'Backend billable-step cap raised 7 -> 12'"},
                        "colliding_rules": {"type": "array", "items": {"type": "string"},
                                            "description": "every retained rule/invariant id or name this change collides with"},
                        "because": {"type": "string",
                                    "description": "one consolidated, cited rationale covering the colliding rules; empty => dropped"},
                    },
                    "required": ["item_refs", "type", "change_summary", "colliding_rules", "because"],
                },
            },
        },
        "required": ["findings"],
    },
}


def build_prompt(changed_items: list, retained: list, claims: list) -> tuple:
    """Return (system, user) prompt strings. Pure; token-bounded payload."""
    system = (
        "You are an architecture reviewer for a pull request. The change has already been "
        "folded into the project's blueprint and rules; you are given a DETERMINISTIC diff "
        "of the source of truth (you do NOT decide what changed). Report CONSOLIDATED findings:\n"
        "- ONE finding per DISTINCT change. If a change spans multiple functions/files "
        "(several changed items), report it ONCE, list every item_ref, and list ALL the "
        "rules it collides with in colliding_rules. NEVER emit a separate finding per rule "
        "or per code symbol — that is noise.\n"
        "- silent_weakening: a REMOVE/UPDATE that retires or softens an invariant or key decision.\n"
        "- contradiction: an ADD/UPDATE that conflicts with a RETAINED rule.\n"
        "- behavior_violates_rule: a described behavior/data change that breaks RETAINED rule(s).\n"
        "Only emit real, cited findings (because-or-suppress); do not flag benign additions. "
        "Prefer FEW, well-consolidated findings. Call emit_findings exactly once."
    )

    def trim(item, n=600):
        s = json.dumps(item, ensure_ascii=False)
        return s if len(s) <= n else s[:n] + "...(truncated)"

    lines = ["CHANGED ITEMS (cite item_ref):"]
    for it in changed_items:
        lines.append(
            f"- ref={it['ref']} layer={it['layer']} op={it['diff_op']} "
            f"section={it['section']} title={it['title']!r}"
        )
        if it.get("base_item") is not None:
            lines.append(f"    base: {trim(it['base_item'])}")
        if it.get("branch_item") is not None:
            lines.append(f"    branch: {trim(it['branch_item'])}")
        if it.get("fields_changed"):
            lines.append(f"    fields_changed: {it['fields_changed']}")
    lines.append("")
    lines.append("RETAINED RULES (must still hold):")
    for r in retained:
        lines.append(f"- {trim(r, 400)}")
    if claims:
        lines.append("")
        lines.append("DECLARED INTENT (sync ledger claims, context only):")
        for c in claims:
            lines.append(f"- kind={c.get('kind')} conf={c.get('confidence')} "
                         f"stmt={str(c.get('statement',''))[:160]!r}")
    return system, "\n".join(lines)


def call_anthropic(system: str, user: str, api_key: str, max_retries: int = 3) -> list:
    """One forced-tool completion returning the raw findings list.

    Name kept for compatibility (contract_delta imports it); routing is now
    provider-agnostic via llm_client (OpenRouter / OpenAI-compatible /
    Anthropic). `api_key` is legacy — the client resolves the real key from
    config/env — but an empty value still means "skip", matching old callers.
    Raises RuntimeError on hard failure.
    """
    try:
        result = llm_client.complete(
            user, system=system, tier="haiku", max_tokens=MAX_TOKENS,
            tools=[EMIT_FINDINGS_TOOL], tool_choice="emit_findings",
            max_retries=max_retries)
    except llm_client.LLMError as e:
        raise RuntimeError(f"LLM API failed: {e}")
    for call in result["tool_calls"]:
        if call["name"] == "emit_findings":
            findings = call["input"].get("findings")
            return findings if isinstance(findings, list) else []
    return []


# ---------------------------------------------------------------------------
# finalize: overwrite deterministic fields, because-or-suppress, ledger join
# ---------------------------------------------------------------------------
def finalize_findings(model_findings: list, changed_items: list, claims: list) -> list:
    """Bind each model finding to the real changed item(s) it spans, derive the
    deterministic fields from the script's own diff, drop unciteable/unmatched findings,
    attach a ledger-confidence sharpener, and merge any findings the model left split.

    A finding is ONE distinct change spanning >=1 changed item, with the full list of
    rules it collides with — so a cap-raise touching two functions and four rules is one
    finding, not eight.
    """
    by_ref = {it["ref"]: it for it in changed_items}
    out = []
    for f in model_findings:
        if not isinstance(f, dict):
            continue
        # accept the consolidated shape (item_refs[]) and the legacy single item_ref.
        refs = f.get("item_refs")
        if not refs and f.get("item_ref"):
            refs = [f["item_ref"]]
        items = [by_ref[r] for r in (refs or []) if r in by_ref]
        if not items:
            continue  # resolves to no real diff item -> drop
        because = str(f.get("because", "")).strip()
        if not because:
            continue  # because-or-suppress

        rules = f.get("colliding_rules")
        if not rules and f.get("rule_name"):
            rules = [f["rule_name"]]
        rules = _dedup_preserve([str(r).strip() for r in (rules or []) if str(r).strip()])
        summary = (str(f.get("change_summary", "")).strip()
                   or str(f.get("what_changed", "")).strip()
                   or items[0]["title"])

        ops = sorted({it["diff_op"] for it in items})
        layers = sorted({it["layer"] for it in items})
        finding = {
            # deterministic, script-owned:
            "diff_op": ops[0] if len(ops) == 1 else "/".join(ops),
            "layer": layers[0],
            "sections": sorted({it["section"] for it in items}),
            "site_count": len(items),
            # model judgment:
            "type": f.get("type", "behavior_violates_rule"),
            "change_summary": summary,
            "colliding_rules": rules,
            "because": because,
            "confidence": None,
        }
        for it in items:  # first conservative ledger-join wins
            join = ledger_join(it, claims)
            if join:
                finding["confidence"] = join.get("confidence")
                finding["reconstructed"] = join.get("reconstructed")
                break
        out.append(finding)
    return _dedupe_findings(out)


def _dedup_preserve(seq):
    seen = set()
    return [x for x in seq if not (x in seen or seen.add(x))]


def _dedupe_findings(findings: list) -> list:
    """Backstop: merge findings the model left split — same type colliding with the SAME
    set of rules is the same logical change. Combines site counts + keeps a confidence."""
    merged = {}
    order = []
    for f in findings:
        if f["colliding_rules"]:
            key = (f["type"], frozenset(r.lower() for r in f["colliding_rules"]))
        else:
            key = (f["type"], f["change_summary"].lower())
        if key in merged:
            m = merged[key]
            m["site_count"] += f["site_count"]
            if f.get("confidence") and not m.get("confidence"):
                m["confidence"] = f.get("confidence")
                m["reconstructed"] = f.get("reconstructed")
        else:
            merged[key] = dict(f)
            order.append(key)
    return [merged[k] for k in order]


# ---------------------------------------------------------------------------
# render + post comment
# ---------------------------------------------------------------------------
_FLAG_HEADERS = {
    "silent_weakening": "⚠️ Silent weakening / removal",
    "contradiction": "⚠️ Contradiction with a retained rule",
    "behavior_violates_rule": "⚠️ Behavior may violate a rule",
}
_FLAG_ORDER = ["silent_weakening", "contradiction", "behavior_violates_rule"]


def sync_advisory(repo_root: Path, base_ref_full: str):
    """(state_present, unsynced) — source files changed in this PR whose CURRENT
    content differs from the last recorded /archie-sync. Content-based, so it is
    immune to commit timing / rebases / squashes. No `.archie/sync_state.json` ->
    every changed source file counts as unsynced. Never raises; on any git failure
    it returns no flags (silence beats a false alarm).

    Selection uses `is_source_path` — the per-path form of the stamp's predicate —
    so the check and the stamp cannot drift (ignored/SKIP_DIRS source files are
    excluded from both), and it stays O(diff): only the changed paths are classified
    and hashed, never the whole tree. A path that no longer exists hashes to None and
    is skipped, so deletions aren't reported as phantom edits. The diff uses -z so
    non-ASCII paths aren't quoted/dropped. Never raises (the whole body is guarded);
    on any git/IO failure it returns no flags — silence beats a false alarm."""
    try:
        code, out, _ = run_git(repo_root, "diff", "--name-only", "-z", f"{base_ref_full}...HEAD")
        if code != 0:
            return (True, [])
        changed = [p for p in out.split("\x00") if p]
        matcher = IgnoreMatcher(repo_root)
        changed_src = [f for f in changed if is_source_path(repo_root, f, matcher)]
        if not changed_src:
            return (True, [])
        state_path = repo_root / ".archie" / "sync_state.json"
        state, _ = _parse_json(state_path.read_text()) if state_path.exists() else (None, None)
        present = isinstance(state, dict) and isinstance(state.get("files"), dict)
        synced = state["files"] if present else {}
        unsynced = []
        for f in changed_src:
            cur = file_sha1(repo_root / f)
            if cur is None:
                continue  # deleted/unreadable — not an unsynced edit
            if cur != synced.get(f):
                unsynced.append(f)
        return (present, unsynced)
    except Exception as e:
        print(f"[intent-review] sync advisory skipped ({e})", file=sys.stderr)
        return (True, [])


def _sync_section(sync_adv) -> str:
    """Render the 'please run /archie-sync' advisory, or '' when nothing to flag."""
    present, unsynced = sync_adv
    if not unsynced:
        return ""
    n = len(unsynced)
    title = ("### 🔄 Living Blueprint may be out of sync" if present
             else "### 🔄 No `/archie-sync` recorded for this branch")
    lead = (f"{n} source file{'s' if n != 1 else ''} changed since the last "
            f"`/archie-sync`" + ("" if present else " (none was recorded)") +
            " — run `/archie-sync` so the blueprint + intent layer reflect this code.")
    listed = "\n".join(f"- `{f}`" for f in unsynced[:10])
    more = f"\n- …and {n - 10} more" if n > 10 else ""
    return f"{title}\n\n{lead}\n\n{listed}{more}\n\n_Advisory — does not block merge._"


def render_comment(findings: list, had_diff: bool, sync_adv, model_failed: bool = False):
    """Markdown body combining the intent review (when the blueprint changed) and
    the sync advisory (when code changed without a re-sync). None -> post nothing.

    When `had_diff` is True the review section ALWAYS renders — as findings, a
    clean-consistent note, or (if `model_failed`) an explicit "could not run"
    notice. It is never silently dropped, so a model failure can't masquerade as a
    clean review, and a later run can't quietly erase a prior review's context."""
    sync_section = _sync_section(sync_adv)
    if not had_diff and not sync_section:
        return None

    lines = [COMMENT_MARKER, "## 📐 Archie Intent Review", ""]
    if had_diff:
        if model_failed:
            lines.append("⚠️ **Intent review could not run** — the model call failed. The "
                         "blueprint changed in this PR but was **not** judged; re-run the "
                         "check or review the source-of-truth changes manually.")
        elif not findings:
            lines.append("No findings — the blueprint changes in this PR are consistent "
                         "with the retained rules.")
        else:
            n = len(findings)
            lines.append(f"This PR changes the architectural source of truth. **{n} finding"
                         f"{'s' if n != 1 else ''}** for a human to weigh:")
            for flag in _FLAG_ORDER:
                group = [f for f in findings if f.get("type") == flag]
                if not group:
                    continue
                lines.append("")
                lines.append(f"### {_FLAG_HEADERS[flag]}")
                for f in group:
                    conf = ""
                    if f.get("confidence"):
                        rec = " · reconstructed guess" if f.get("reconstructed") else ""
                        conf = f" _(ledger confidence: {f['confidence']}{rec})_"
                    sites = f" · {f['site_count']} sites" if f.get("site_count", 1) > 1 else ""
                    collides = ""
                    if f.get("colliding_rules"):
                        collides = "  \n  Collides with: **" + ", ".join(f["colliding_rules"]) + "**"
                    lines.append(
                        f"- **{f['change_summary']}** ({f['diff_op']}, Layer {f['layer']}{sites}){conf}{collides}  \n"
                        f"  _Because:_ {f['because']}"
                    )

    if sync_section:
        lines.append("")
        lines.append(sync_section)

    lines.append("")
    if had_diff and not model_failed and findings:
        lines.append("*Archie surfaces; it doesn't block. Whether a change means \"fix the "
                     "code\" or \"evolve the rule\" is your call — merge accepts the blueprint "
                     "changes above as the new baseline.*")
    else:
        lines.append("*Archie surfaces; it doesn't block.*")
    return "\n".join(lines)


def _gh_request(method: str, url: str, token: str, body: dict = None):
    """One GitHub REST call. urllib raises HTTPError on >=400, so non-2xx is NOT a
    silent success. Returns (data, link_header). Raises on transport/HTTP errors —
    callers that must never block use safe_post_comment().
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "archie-intent-review",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        link = resp.headers.get("Link", "") if resp.headers else ""
    return (json.loads(raw) if raw.strip() else {}), link


def _next_link(link_header: str):
    """Parse a GitHub `Link` header for the rel="next" URL, or None."""
    for part in (link_header or "").split(","):
        segs = part.split(";")
        if len(segs) >= 2 and 'rel="next"' in segs[1]:
            return segs[0].strip().strip("<>")
    return None


def _find_existing_comment_id(owner, repo, pr_number, token, marker=COMMENT_MARKER):
    """Find THIS script's comment by ITS marker, following pagination (PRs with >100
    comments won't cause a duplicate POST).

    The marker is a PARAMETER because two scripts post to the same PR. When both
    looked up by this module's COMMENT_MARKER, delivery_review PATCHed over the
    intent review's comment on every run — leaking one orphaned comment per run.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments?per_page=100"
    while url:
        comments, link = _gh_request("GET", url, token)
        for c in comments if isinstance(comments, list) else []:
            if marker in (c.get("body") or ""):
                return c.get("id")
        url = _next_link(link)
    return None


def post_or_update_comment(owner, repo, pr_number, body, token, marker=COMMENT_MARKER):
    """Upsert THIS script's Archie comment (find by its marker -> PATCH, else POST).
    May raise (HTTPError/URLError); callers in CI must use safe_post_comment()."""
    existing_id = _find_existing_comment_id(owner, repo, pr_number, token, marker=marker)
    if existing_id:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{existing_id}"
        _gh_request("PATCH", url, token, {"body": body})
        print(f"[intent-review] updated comment {existing_id}")
    else:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        _gh_request("POST", url, token, {"body": body})
        print("[intent-review] posted new comment")


def safe_post_comment(owner, repo, pr_number, body, token, marker=COMMENT_MARKER):
    """Post but NEVER raise — the Action must always exit 0 (design §9). Catches every
    network/HTTP error (URLError covers HTTPError; OSError covers socket failures)."""
    if not token:
        print("[intent-review] no GITHUB_TOKEN — skipping comment post.", file=sys.stderr)
        return
    try:
        post_or_update_comment(owner, repo, pr_number, body, token, marker=marker)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[intent-review] could not post comment: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# event context
# ---------------------------------------------------------------------------
def parse_event_context(env: dict):
    """Return (owner, repo, pr_number, base_ref, base_sha) or None.

    `base_sha` (pull_request.base.sha) is the robust base to diff against: with
    `actions/checkout` `fetch-depth: 0` it is always present in the merge-ref history,
    so no `git fetch` is needed and there is no `origin/<base>` resolution to fail.
    """
    repo_full = env.get("GITHUB_REPOSITORY", "")
    base_ref = env.get("GITHUB_BASE_REF", "")
    event_path = env.get("GITHUB_EVENT_PATH", "")
    if "/" not in repo_full:
        return None
    owner, repo = repo_full.split("/", 1)
    pr_number = None
    base_sha = ""
    if event_path and Path(event_path).exists():
        try:
            event = json.loads(Path(event_path).read_text())
            pr = event.get("pull_request")
            if isinstance(pr, dict):
                pr_number = pr.get("number")
                base = pr.get("base") or {}
                base_ref = base_ref or base.get("ref", "")
                base_sha = base.get("sha", "") or ""
        except (OSError, json.JSONDecodeError):
            return None
    if pr_number is None or not base_ref:
        return None
    return owner, repo, pr_number, base_ref, base_sha


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    repo_root = Path(os.environ.get("GITHUB_WORKSPACE") or ".").resolve()
    env = os.environ

    # 1. Fork-PR / no-secret guard FIRST — before any GitHub write.
    if llm_client.resolve_config() is None:
        print("[intent-review] no LLM provider configured (set OPENROUTER_API_KEY "
              "or ANTHROPIC_API_KEY) (fork PR?) — skipping.", file=sys.stderr)
        return 0
    api_key = env.get("ANTHROPIC_API_KEY", "").strip() or env.get("OPENROUTER_API_KEY", "").strip()

    ctx = parse_event_context(env)
    if ctx is None:
        print("[intent-review] not a usable pull_request event — skipping.", file=sys.stderr)
        return 0
    owner, repo, pr_number, base_ref, base_sha = ctx
    token = env.get("GITHUB_TOKEN", "").strip()
    # Prefer the base SHA (always in merge-ref history with fetch-depth:0); fall back to
    # origin/<base> only if the payload lacked a sha. No `git fetch` is required.
    base_ref_full = base_sha or f"origin/{base_ref}"

    # Sync advisory — computed FIRST and independently of the blueprint, so it still
    # surfaces when the branch blueprint is absent or malformed (exactly when a "you
    # never synced" nudge matters most). It needs no blueprint; on git failure it
    # returns nothing.
    sync_adv = sync_advisory(repo_root, base_ref_full)

    # 2. Load branch + base versions of the source of truth.
    b_exists, branch_bp, b_err = load_branch_file(repo_root, ".archie/blueprint.json")
    if b_exists and branch_bp is None:
        # branch blueprint is malformed — surface (with the sync advisory), don't crash.
        body = (f"{COMMENT_MARKER}\n## 📐 Archie Intent Review\n\n"
                f"Could not parse `.archie/blueprint.json` on this branch ({b_err}). "
                f"Manual review needed.")
        sec = _sync_section(sync_adv)
        if sec:
            body += "\n\n" + sec
        safe_post_comment(owner, repo, pr_number, body, token)
        return 0
    if not b_exists:
        # No blueprint to review — but still nudge if source drifted without a sync.
        body = render_comment([], False, sync_adv)
        if body is None:
            print("[intent-review] no .archie/blueprint.json on branch — nothing to review.", file=sys.stderr)
        elif token:
            safe_post_comment(owner, repo, pr_number, body, token)
        else:
            print("[intent-review] no GITHUB_TOKEN — printing body:\n" + body)
        return 0

    base_exists, base_bp, base_err = fetch_base_file(repo_root, base_ref_full, ".archie/blueprint.json")
    if base_err:
        # The base REF could not be resolved (not "file absent"). Do NOT silently degrade
        # to an empty baseline and post a confident-but-wrong "everything is new" review.
        print(f"[intent-review] base ref {base_ref_full} unresolvable: {base_err}", file=sys.stderr)
        safe_post_comment(owner, repo, pr_number,
                          f"{COMMENT_MARKER}\n## 📐 Archie Intent Review\n\n"
                          f"Could not resolve the PR base (`{base_ref_full}`) to diff against "
                          f"(`{base_err}`). **Review skipped** to avoid a misleading "
                          f"\"everything is new\" result — re-run once the base ref is available.",
                          token)
        return 0
    base_bp = base_bp if isinstance(base_bp, dict) else {}

    # Diff BOTH rule sources (rules.json + platform_rules.json), unioned.
    base_rules, branch_rules = [], []
    for rel in RULE_FILES:
        _, base_raw, _ = fetch_base_file(repo_root, base_ref_full, rel)
        _, branch_raw, _ = load_branch_file(repo_root, rel)
        base_rules.extend(normalize_rules(base_raw))
        branch_rules.extend(normalize_rules(branch_raw))

    claims = glob_ledger(repo_root, base_ref_full)

    # 3. Deterministic diff -> changed items.
    changed_items = build_changed_items(base_bp, branch_bp, base_rules, branch_rules, claims)
    had_diff = bool(changed_items)

    # 4. Judge with one model call — only when the source of truth actually moved.
    findings, model_failed = [], False
    if had_diff:
        retained = retained_rules(base_rules, changed_items)
        system, user = build_prompt(changed_items, retained, claims)
        try:
            model_findings = call_anthropic(system, user, api_key)
            findings = finalize_findings(model_findings, changed_items, claims)
        except RuntimeError as e:
            # Never block — but render an explicit "could not run" notice (the
            # blueprint DID change) instead of a silent clean-looking review.
            print(f"[intent-review] model call failed: {e}", file=sys.stderr)
            model_failed = True
    else:
        print("[intent-review] no source-of-truth changes detected.", file=sys.stderr)

    # 5. Render + upsert — posts if EITHER the review or the sync advisory has content.
    body = render_comment(findings, had_diff, sync_adv, model_failed)
    if body is None:
        print("[intent-review] nothing to surface — posting nothing.", file=sys.stderr)
        return 0
    if not token:
        print("[intent-review] no GITHUB_TOKEN — printing body:\n" + body)
        return 0
    safe_post_comment(owner, repo, pr_number, body, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
