"""PR-gate delivery review: intake + full A/B/C reconciliation + verdict comment.
Reuses intent_review.post_or_update_comment for the upsert. Diffing, intent, and
gating come from the shared core.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
from pathlib import Path

_p = str(Path(__file__).parent)
if _p not in sys.path:
    sys.path.insert(0, _p)

_OVERRIDE_LABEL = "archie-review"
_SKIP_LABEL = "archie-skip"

# Default cap on changed files before a PR is considered too large to review.
MAX_FILES = 75
# Bound the diff payload sent to the LLM (chars) so a huge PR can't blow the budget.
MAX_DIFF_CHARS = 60000
DELIVERY_MARKER = "<!-- archie-delivery-review -->"

# Zero-width space used to neutralize leading @ in model-derived text.
_ZWS = "​"


def _sanitize(text: object) -> str:
    """Sanitize a model-derived field before embedding in a Markdown comment.

    - Coerces to str.
    - HTML-escapes <, >, & so injected HTML comment markers (<!--/-->) become
      harmless entities (&lt;!-- / --&gt;) and raw HTML tags are neutralized.
    - Collapses newlines/carriage returns to spaces so a field cannot inject a
      second Markdown block / fake verdict heading on its own line.
    - Neutralizes @ ANYWHERE (not just token-start) to prevent live GitHub
      @mention notifications (e.g. "(@everyone)", ".@here").
    """
    s = html.escape(str(text) if text is not None else "")
    # Collapse newlines/CRs to spaces — no field may open a new Markdown line.
    s = s.replace("\r", " ").replace("\n", " ")
    # Neutralize EVERY @ so no live mention can survive regardless of position.
    s = s.replace("@", "@" + _ZWS)
    return s


def should_review(pr_meta: dict, max_files: int) -> tuple[bool, str]:
    """Determine if a PR should receive a delivery review.

    Returns (eligible: bool, reason: str).

    Priority order:
    1. Override label forces True even for bots.
    2. Bot author → False.
    3. Skip label → False.
    4. Too many changed files → False.
    5. Otherwise → True.
    """
    labels = pr_meta.get("labels") or []
    if _OVERRIDE_LABEL in labels:
        return True, "override label"
    if str(pr_meta.get("author", "")).endswith("[bot]"):
        return False, "bot author"
    if _SKIP_LABEL in labels:
        return False, "skip label"
    if int(pr_meta.get("changed_files") or 0) > max_files:
        return False, "too many files"
    return True, "eligible"


_LENS_ORDER = ["security", "concurrency", "resource-perf", "null-safety"]
_JUDGE_HEADERS = {
    "silent_weakening": "⚠️ Silent weakening / removal",
    "contradiction": "⚠️ Contradiction with a retained rule",
    "behavior_violates_rule": "⚠️ Behavior may violate a rule",
}
_JUDGE_ORDER = ["silent_weakening", "contradiction", "behavior_violates_rule"]


def _clip(text: str, n: int = 160) -> str:
    """Table cells hold the user's own prose from the confirm prompt, which can run
    to a paragraph. Keep the gist; a 400-char cell is unreadable."""
    t = str(text or "").strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + " …"


def _lens_of(f) -> str:
    """The reviewer that produced this finding: 'universal:security' -> 'security'."""
    return str(f.get("source", "")).split(":")[-1] or "other"


def _clean_anchor(f) -> tuple:
    """(path, line) from a possibly-messy anchor. Models sometimes stuff prose into
    anchor.file (e.g. "supabase.ts:line 236: `Number(x)`" or "line 233-237 (logic)"),
    which renders as junk. Pull the first path-looking token and the first line
    number, from either the file field or the whole anchor."""
    a = f.get("anchor") or {}
    raw = str(a.get("file", ""))
    line = a.get("line")
    m = re.search(r"[\w][\w./-]*\.[A-Za-z0-9]+", raw)
    path = m.group(0) if m else (raw.split(":")[0].split()[0] if raw.split() else raw)
    if line in (None, "", "?"):
        lm = re.search(r":?\s*(?:line\s*)?(\d+)", raw[len(path):] if path in raw else raw)
        line = lm.group(1) if lm else ""
    return path, str(line)


def _anchor_md(f, link_base) -> str:
    """A clean, clickable `path:line`. link_base like
    'https://github.com/OWNER/REPO/blob/SHA' -> a blob link; else a code span."""
    path, line = _clean_anchor(f)
    label = _sanitize(f"{path}:{line}" if line else path)
    if link_base and path:
        frag = f"#L{line}" if line and line.isdigit() else ""
        return f"[`{label}`]({link_base}/{path}{frag})"
    return f"`{label}`"


def render_verdict(verdict: dict, confirmed: list, spec=None,
                   retired=None, judged=None, unauthorized=None) -> str:
    """Render the single PR comment.

    Order is the product. The contract delta is what merging ACCEPTS, so it leads:
    the laws the user authorized retiring (with their reason), plus any change
    /archie-sync made that nobody authorized, judged against the retained rules. Code
    review follows as the evidence a human acts on. There is no intent grading — the
    acceptance criteria were extracted from the PR body, so grading the PR against
    them was circular.
    """
    spec = spec or {}
    retired = retired or []
    judged = judged or {}
    unauthorized = unauthorized or []
    j_items = judged.get("items") or []
    j_findings = judged.get("findings") or []
    j_failed = bool(judged.get("model_failed"))
    engine_failed = bool(spec.get("review_engine_failed"))
    link_base = spec.get("link_base") or ""

    # Rules being retired IN THIS PR (id + the invariants they ground). A judge
    # collision against one of these is not a real conflict — the rule is on its
    # way out in the same diff — so we annotate rather than alarm.
    retired_ids = set()
    for c in retired:
        retired_ids.add(c.get("rule_id"))
        retired_ids.update(c.get("invariant_ids") or [])

    lines = [DELIVERY_MARKER, "## Archie review", ""]
    if engine_failed:
        lines += ["> 🛑 **REVIEW ENGINE FAILED — no code review was performed.** "
                  "Do NOT treat this comment as a green verdict. Check the workflow "
                  "logs for `[archie] review core failed`.", ""]

    # The developer's own account of what this PR is for. Captured by the task-story
    # imprint (or the PR body); shown as context, never graded.
    story = _sanitize((spec.get("story") or "").strip())
    if story:
        lines += ["<details><summary>📝 What this PR set out to do</summary>", "",
                  story, "", "</details>", ""]

    # ---- contract: what merging ACCEPTS (retirements the user authorized) ----
    if retired:
        lines += [f"### ⛔ Contract changes — merging this PR accepts them ({len(retired)})", "",
                  "| Law | Change | Why | Authorized |", "|---|---|---|---|"]
        for c in retired:
            law = _sanitize(_clip(c.get("law", ""), 110)) or "_(law text unavailable)_"
            lines.append(
                f"| `{_sanitize(c.get('rule_id', '?'))}` {law} | **RETIRE** | "
                f"{_sanitize(_clip(c.get('reason', ''), 140))} | "
                f"{_sanitize(c.get('authorized_by', '?'))}, "
                f"{_sanitize(c.get('date', ''))} |")
        lines.append("")

    # ---- unexpected source-of-truth changes nobody authorized (judged) ----
    if j_items:
        n = len(j_items)
        lines += [f"### 🔎 Unexpected source-of-truth changes ({n})", "",
                  "_Edits to `rules.json` / `blueprint.json` that no override authorized "
                  "(usually folded in by `/archie-sync`) — checked against the retained rules._",
                  ""]
        if j_failed:
            lines += ["⚠️ **Could not be judged** — the model call failed. The source of "
                      "truth changed but was **not** checked against the retained rules. "
                      "Re-run the check or review the diff manually.", ""]
        elif not j_findings:
            lines += ["✅ Consistent with the retained rules.", ""]
        else:
            for flag in _JUDGE_ORDER:
                group = [f for f in j_findings if f.get("type") == flag]
                if not group:
                    continue
                lines.append(f"**{_JUDGE_HEADERS[flag]}**")
                for f in group:
                    collides = ""
                    if f.get("colliding_rules"):
                        parts = []
                        for r in f["colliding_rules"]:
                            tag = " _(retired in this PR)_" if r in retired_ids else ""
                            parts.append(f"**{_sanitize(r)}**{tag}")
                        collides = " · collides with " + ", ".join(parts)
                    lines.append(f"- {_sanitize(f.get('change_summary', ''))} "
                                 f"({_sanitize(f.get('diff_op', ''))}, "
                                 f"Layer {_sanitize(str(f.get('layer', '')))}){collides}")
                lines.append("")

    if retired or j_items:
        lines += ["*After merge these changes become the contract. The next deep scan "
                  "re-derives this area from the code.*", ""]

    def _finding(f):
        return f"- {_anchor_md(f, link_base)} — {_sanitize(f.get('problem_statement', ''))}"

    if unauthorized:
        lines += [f"### 🚨 Unauthorized law violations ({len(unauthorized)}) — "
                  "not covered by any override", ""]
        lines += [_finding(f) for f in unauthorized] + [""]

    if engine_failed:
        lines += ["### 🔍 Code review", "", "_not assessed — no reviewer ran._", ""]
    else:
        by_lens = {}
        for f in confirmed:
            by_lens.setdefault(_lens_of(f), []).append(f)
        ordered = [l for l in _LENS_ORDER if l in by_lens] + \
                  sorted(k for k in by_lens if k not in _LENS_ORDER)
        lines += [f"### 🔍 Code review — {len(confirmed)} findings", ""]
        nf = int(spec.get("reviewers_failed") or 0)
        if nf:
            lines += [f"> ⚠️ **Coverage incomplete** — {nf} of "
                      f"{spec.get('reviewers_total', '?')} reviewers failed (timeout or API "
                      "error). Findings below are a lower bound; see the workflow log for "
                      "`[archie] reviewer failed`.", ""]
        if not confirmed:
            lines.append("_No defects found._")
        # Group by lens; collapse each group so 11 findings don't wall the reader,
        # but the first (highest-priority) lens stays open.
        for i, lens in enumerate(ordered):
            fs = by_lens[lens]
            body = [_finding(f) for f in fs]
            openattr = " open" if i == 0 else ""
            lines += [f"<details{openattr}><summary><b>{lens}</b> ({len(fs)})</summary>", ""]
            lines += body
            lines += ["", "</details>", ""]

    lines += ["---",
              f"*Contract: {len(retired)} retired · {len(j_items)} unexpected · "
              f"{len(unauthorized)} unauthorized · Code: {len(confirmed)} finding(s).*"]
    return "\n".join(lines)


def split_findings(findings):
    """(code_review, unauthorized). Conformance findings are law violations; the
    specialist skips acked laws, so any survivor is uncovered by an override."""
    unauth = [f for f in findings if f.get("kind") == "conformance_break"]
    code = [f for f in findings if f.get("kind") != "conformance_break"]
    return code, unauth


def partition_for_verdict(root, confirmed):
    """Split confirmed findings by human ruling (overrides.partition). Degrades
    to (confirmed, [], []) when the overrides module/file is absent — the exit-0
    contract must survive a missing ledger."""
    try:
        import overrides as _ov
        act = _ov.active(root)
        if not act:
            return confirmed, [], []
        return _ov.partition(confirmed, act, root=root)
    except Exception:
        return confirmed, [], []


def _load_pr_meta_from_event(event_path):
    """Read the GitHub event payload -> PR metadata dict.

    Returns {author, changed_files, labels, number, base_ref, base_sha, head_sha}.
    Never raises: on any error returns an empty-ish dict so the caller degrades
    gracefully (no PR context -> nothing to review).
    """
    meta = {"author": "", "changed_files": 0, "labels": [], "number": None,
            "base_ref": "", "base_sha": "", "head_sha": "", "head_ref": "",
            "title": "", "body": ""}
    if not event_path or not Path(event_path).exists():
        return meta
    try:
        event = json.loads(Path(event_path).read_text())
    except Exception:
        return meta
    pr = event.get("pull_request") or {}
    if isinstance(pr, dict):
        meta["number"] = pr.get("number")
        meta["changed_files"] = pr.get("changed_files") or 0
        user = pr.get("user") or {}
        meta["author"] = str(user.get("login", "")) if isinstance(user, dict) else ""
        base = pr.get("base") or {}
        head = pr.get("head") or {}
        meta["base_ref"] = str(base.get("ref", "") or "")
        meta["base_sha"] = str(base.get("sha", "") or "")
        meta["head_sha"] = str(head.get("sha", "") or "")
        meta["head_ref"] = str(head.get("ref", "") or "")
        meta["title"] = str(pr.get("title", "") or "")
        meta["body"] = str(pr.get("body", "") or "")
        labels = pr.get("labels") or []
        meta["labels"] = [l.get("name") for l in labels
                          if isinstance(l, dict) and l.get("name")]
    return meta


def assemble_pr_intent(root, pr_meta, env, *, run=None):
    """Merge intent from current task story ⊕ PR title/body.
    Falls back to PR-only intent when there is no active story.
    resolve() runs ONLY if the merged spec still has no acceptance_criteria."""
    import sys as _sys
    _p = str(Path(__file__).parent)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
    from intent import (merge_specs, normalize,  # noqa: E402
                        resolve, ticket_ids_from)
    import story_store  # noqa: E402

    # Resolve the branch: PR meta first, then env vars used by CI providers.
    branch = (pr_meta.get("head_ref")
              or os.environ.get("ARCHIE_BRANCH")
              or os.environ.get("GITHUB_HEAD_REF")
              or "")

    story = story_store.current_story(root, branch)
    committed = {"acceptance_criteria": [], "non_goals": [], "source": "sync",
                 "confirmed": False, "story": ""}
    if story:
        committed["acceptance_criteria"] = [
            {"id": f.get("id"), "text": f.get("text", ""), "from": f.get("from")}
            for f in story["facts"]
        ]
        committed["non_goals"] = story.get("non_goals") or []
        committed["confirmed"] = bool(story["meta"].get("confirmed"))
        committed["story"] = story.get("story") or ""

    title = pr_meta.get("title") or ""
    body = pr_meta.get("body") or env.get("ARCHIE_PR_BODY", "")
    pr_text = (title + "\n\n" + body).strip()
    tickets = ticket_ids_from(branch, pr_text, [])
    pr_spec = normalize(pr_text, "pr_body", tickets) if pr_text else None

    # Use committed story spec as the base; merge PR title/body on top.
    file_spec = committed if (committed["acceptance_criteria"] or committed["story"]) else None
    spec = merge_specs(file_spec, pr_spec)
    if not spec.get("acceptance_criteria") and spec.get("raw"):
        try:
            spec = resolve(spec, run=run)
        except Exception as e:
            print(f"[archie] intent resolve failed ({e})")
    # Propagate story fields that merge_specs doesn't know about.
    if committed["story"] and not spec.get("story"):
        spec["story"] = committed["story"]
    if committed["non_goals"] and not spec.get("non_goals"):
        spec["non_goals"] = committed["non_goals"]
    # merge_specs rebuilds criteria as {id, text} and drops per-fact provenance.
    # Re-attach `from` by matching on normalized text so the verdict can render it.
    _prov = {}
    for _c in (committed.get("acceptance_criteria") or []):
        if _c.get("from"):
            _prov[(_c.get("text") or "").strip().lower()] = _c["from"]
    for _c in (spec.get("acceptance_criteria") or []):
        _k = (_c.get("text") or "").strip().lower()
        if _k in _prov and not _c.get("from"):
            _c["from"] = _prov[_k]
    return spec


def run_pr_gate(root=".", env=None):
    """Compose the PR gate: intake -> diff -> resolve intent -> reconcile (A/B/C)
    + behavioral -> gate -> verdict -> comment.

    NON-BLOCKING by contract: every external step (git, LLM, GitHub API) is guarded,
    a failure prints a line and continues, and the function NEVER raises. Returns a
    small status dict for callers/tests.
    """
    env = env if env is not None else os.environ
    root = Path(root)
    status = {"reviewed": False, "reason": "", "posted": False, "verdict": None}

    # 1. Read PR context from the GitHub event payload.
    pr_meta = _load_pr_meta_from_event(env.get("GITHUB_EVENT_PATH", ""))

    # 2. Intake gate — skip bots / skip-label / too-many-files (override-label wins).
    ok, reason = should_review(pr_meta, MAX_FILES)
    status["reason"] = reason
    if not ok:
        print(f"[archie] delivery review skipped ({reason})")
        return status
    if not pr_meta.get("number"):
        print("[archie] delivery review skipped (no PR context)")
        status["reason"] = "no pr context"
        return status

    # 3. Diff basis — provider base SHA when present, else detect. Bounded diff text.
    diff_text, changed, changed_lines, spec_truncated = "", [], {}, False
    try:
        from diff_basis import detect_base, changed_files_result, parse_hunk_added_lines, review_pathspec
        base = pr_meta.get("base_sha") or pr_meta.get("base_ref") or detect_base(root)
        res = changed_files_result(root, base)
        changed = res.get("files", []) if res.get("ok") else []
        try:
            from intent_review import run_git
            _, out, _ = run_git(root, "diff", base, "--", *review_pathspec())
            diff_text = (out or "")[:MAX_DIFF_CHARS]
            spec_truncated = len(out or "") > MAX_DIFF_CHARS
            # changed-line map: parse a -U0 diff into the REAL added-line numbers per
            # file so the editor gate keeps line-anchored findings on changed lines
            # (instead of dropping all of them as anchor_unchanged). On any failure
            # fall back to {} — the gate then skips the anchor check (findings survive).
            try:
                import subprocess
                r = subprocess.run(
                    ["git", "-C", str(root), "diff", "-U0", base, "--", *review_pathspec()],
                    capture_output=True, text=True, timeout=30,
                )
                changed_lines = (parse_hunk_added_lines(r.stdout)
                                 if r.returncode == 0 else {})
            except Exception as e:
                print(f"[archie] changed-line parse failed ({e})")
                changed_lines = {}
        except Exception as e:
            print(f"[archie] diff read failed ({e})")
    except Exception as e:
        print(f"[archie] diff basis failed ({e})")

    # 4. Assemble intent: current task story ⊕ PR title/body.
    try:
        spec = assemble_pr_intent(root, pr_meta, env)
    except Exception as e:
        print(f"[archie] intent assembly failed ({e})")
        spec = {"acceptance_criteria": [], "goals": [], "confidence": "low"}

    # 5. Load the blueprint (for edge-C invariants + behavioral blast radius).
    blueprint, import_graph = {}, {}
    try:
        bp_path = root / ".archie" / "blueprint.json"
        if bp_path.exists():
            blueprint = json.loads(bp_path.read_text()) or {}
        scan_path = root / ".archie" / "scan.json"
        if scan_path.exists():
            import_graph = (json.loads(scan_path.read_text()) or {}).get("import_graph", {})
    except Exception as e:
        print(f"[archie] blueprint load failed ({e})")

    # 5b. Contract delta — deterministic authorized retirements + (only when the
    #     source of truth moved in ways nobody authorized) one Haiku judgment.
    retired, judged = [], {}
    try:
        import contract_delta as _cd
        import llm_client as _llm
        retired = _cd.retirements(root)
        base_ref_full = pr_meta.get("base_sha") or f"origin/{env.get('GITHUB_BASE_REF', '')}"
        api_key = (_llm.resolve_config(root, env) or {}).get("api_key", "")
        judged = _cd.judged_changes(root, base_ref_full, api_key)
    except Exception as e:
        print(f"[archie] contract delta failed ({e})")

    # 6. Run the reviewers via the shared core (evidence pack + parallel fan-out + merge).
    #    One core, shared with the local sync review (F3). Guarded — a core failure
    #    degrades to no findings, never aborts the gate — but it must be DISCLOSED:
    #    a crashed engine once rendered as a glowing 13/13, 0-break verdict (PR #17).
    raw = []
    engine_failed = False
    review_stats = {}
    try:
        from review_core import run_review
        if spec_truncated:
            spec["diff_truncated"] = True
        raw = run_review(root, diff_text, changed, blueprint, import_graph, spec,
                         stats=review_stats)
    except Exception as e:
        engine_failed = True
        print(f"[archie] review core failed ({e})")

    # 7. Editor gate + aggregate verdict.
    confirmed = []
    unauthorized = []
    verdict = {"intent_completeness": "0/0", "breaks": 0, "conflicts": 0, "gate_signal": 1.0}
    try:
        from editor_gate import gate
        from reconcile import aggregate_verdict
        store = []
        fp = root / ".archie" / "findings.json"
        if fp.exists():
            try:
                store = json.loads(fp.read_text()).get("findings", [])
            except Exception:
                store = []
        # Advisory code-review kinds anchor file-level (LLM line numbers are imprecise)
        # and carry no confidence floor — surface what the reviewer finds, then split
        # by confidence at render time into breaks vs "possible issues".
        advisory = {"behavioral_break", "conformance_break"}
        floors = {k: 0.0 for k in advisory}
        cl = changed_lines or None
        result = gate(raw, store, changed_lines=cl, floors=floors, file_level_kinds=advisory)
        confirmed = result.get("confirmed", [])
        confirmed, unauthorized = split_findings(confirmed)
        verdict = aggregate_verdict(spec, confirmed)
    except Exception as e:
        # A gate/verdict crash silently discards every finding — that is an
        # engine failure for the reader's purposes (PR #17 rendered green this
        # way twice, through two different holes). Fail closed here too.
        engine_failed = True
        print(f"[archie] gate/verdict failed ({e})")

    # Fail CLOSED at render time: a dead engine must never present as a clean
    # review. render_verdict reads this flag and marks the code-review section
    # "not assessed" instead of rendering an empty (green-looking) one. The
    # contract delta is deterministic, so it still renders.
    if engine_failed:
        spec["review_engine_failed"] = True
    spec["reviewers_failed"] = review_stats.get("failed", 0)
    spec["reviewers_total"] = review_stats.get("total", 0)
    # Clickable file:line anchors — blob links against the PR head SHA when known.
    _repo = env.get("GITHUB_REPOSITORY", "")
    _sha = pr_meta.get("head_sha") or pr_meta.get("head_ref") or ""
    _server = (env.get("GITHUB_SERVER_URL", "") or "https://github.com").rstrip("/")
    if _repo and _sha:
        spec["link_base"] = f"{_server}/{_repo}/blob/{_sha}"

    status["reviewed"] = not engine_failed
    status["verdict"] = verdict

    # 8. Render + publish. Fork PRs (no token) print the verdict instead of posting.
    # A render/post failure must never abort the review (exit 0 by contract).
    try:
        body = render_verdict(verdict, confirmed, spec, retired=retired,
                              judged=judged, unauthorized=unauthorized)
        token = env.get("GITHUB_TOKEN", "").strip()
        repo_full = env.get("GITHUB_REPOSITORY", "")
        number = pr_meta.get("number")
        if token and number and "/" in repo_full:
            owner, repo = repo_full.split("/", 1)
            try:
                from intent_review import post_or_update_comment
                post_or_update_comment(owner, repo, number, body, token,
                                       marker=DELIVERY_MARKER)
                status["posted"] = True
            except Exception as e:
                print(f"[archie] could not post comment ({e})")
                print(body)
        else:
            print("[archie] no GITHUB_TOKEN / PR — printing verdict:\n" + body)
    except Exception as e:
        print(f"[archie] render/publish failed ({e})")

    return status


if __name__ == "__main__":
    try:
        run_pr_gate(os.getcwd(), os.environ)
    except Exception as e:
        print(f"[archie] delivery review skipped (error: {e})")
    # Non-blocking by design: always exit 0.
    sys.exit(0)
