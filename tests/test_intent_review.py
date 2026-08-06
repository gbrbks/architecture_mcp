"""Tests for intent_review.py — the PR-time semantic review.

Covers the deterministic, network-free core: keyed diff, base-ref fetch, ledger glob,
event parsing, the conservative ledger join, the deterministic-field overwrite +
because-or-suppress filter, and comment rendering. The two network calls
(call_anthropic, post_or_update_comment) are exercised with monkeypatched urllib.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_STANDALONE = Path(__file__).resolve().parent.parent / "archie" / "standalone"
sys.path.insert(0, str(_STANDALONE))

import intent_review as ir  # noqa: E402


# ---------------------------------------------------------------------------
# git helpers (mirror test_sync.py)
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "Tester")
    _git(tmp_path, "checkout", "-q", "-b", "main")
    return tmp_path


def _write(root: Path, rel: str, data) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) if not isinstance(data, str) else data)


def _commit(root: Path, msg: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)


# ---------------------------------------------------------------------------
# normalize_rules
# ---------------------------------------------------------------------------
def test_normalize_rules_shapes():
    assert ir.normalize_rules({"rules": [{"id": "r1"}]}) == [{"id": "r1"}]
    assert ir.normalize_rules([{"id": "r1"}]) == [{"id": "r1"}]
    assert ir.normalize_rules(None) == []
    assert ir.normalize_rules({}) == []
    assert ir.normalize_rules({"rules": "bad"}) == []
    assert ir.normalize_rules(42) == []


# ---------------------------------------------------------------------------
# keyed_diff
# ---------------------------------------------------------------------------
def test_keyed_diff_remove_update_add():
    base = [{"id": "a", "v": 1}, {"id": "b", "v": 1}]
    branch = [{"id": "b", "v": 2}, {"id": "c", "v": 1}]
    diffs = {d["key"]: d for d in ir.keyed_diff(base, branch, "id", "v")}
    assert diffs["a"]["status"] == "REMOVE"
    assert diffs["b"]["status"] == "UPDATE"
    assert "v" in diffs["b"]["fields_changed"]
    assert diffs["c"]["status"] == "ADD"


def test_keyed_diff_reorder_is_noop():
    base = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    branch = [{"id": "b", "v": 2}, {"id": "a", "v": 1}]
    assert ir.keyed_diff(base, branch, "id", "v") == []


def test_keyed_diff_title_hash_fallback():
    # no id field -> keyed on hash of the title field
    base = [{"title": "Tenant isolation", "body": "x"}]
    branch = [{"title": "Tenant isolation", "body": "y"}]
    diffs = ir.keyed_diff(base, branch, None, "title")
    assert len(diffs) == 1
    assert diffs[0]["status"] == "UPDATE"
    assert diffs[0]["key"] == ir._hash_title("Tenant isolation")


def test_keyed_diff_handles_missing_or_nonlist():
    assert ir.keyed_diff(None, None, "id", "t") == []
    add_only = ir.keyed_diff([], [{"id": "x"}], "id", "t")
    assert add_only[0]["status"] == "ADD"


# ---------------------------------------------------------------------------
# fetch_base_file / load_branch_file
# ---------------------------------------------------------------------------
def test_fetch_base_file_present_and_absent(tmp_path):
    root = _init_repo(tmp_path)
    _write(root, ".archie/blueprint.json", {"domain_invariants": [{"id": "d1"}]})
    _commit(root, "base")

    exists, data, err = ir.fetch_base_file(root, "main", ".archie/blueprint.json")
    assert exists and err is None
    assert data["domain_invariants"][0]["id"] == "d1"

    # absent file on the ref -> (False, None, None) => treat as all-ADD
    exists, data, err = ir.fetch_base_file(root, "main", ".archie/rules.json")
    assert exists is False and data is None and err is None


def test_fetch_base_file_unresolvable_ref_is_error_not_absent(tmp_path):
    # A bad SHA must NOT be mistaken for "file absent" (the silent-degradation trap).
    root = _init_repo(tmp_path)
    _write(root, ".archie/blueprint.json", {"domain_invariants": []})
    _commit(root, "base")
    exists, data, err = ir.fetch_base_file(root, "deadbeefdeadbeef", ".archie/blueprint.json")
    assert exists is False and data is None and err  # error surfaced, not (False,None,None)


def test_fetch_base_file_malformed(tmp_path):
    root = _init_repo(tmp_path)
    _write(root, ".archie/blueprint.json", "{not valid json")
    _commit(root, "bad")
    exists, data, err = ir.fetch_base_file(root, "main", ".archie/blueprint.json")
    assert exists is True and data is None and err is not None


def test_load_branch_file_missing_and_empty(tmp_path):
    root = _init_repo(tmp_path)
    exists, data, err = ir.load_branch_file(root, ".archie/rules.json")
    assert exists is False and data is None
    _write(root, ".archie/rules.json", "")
    exists, data, err = ir.load_branch_file(root, ".archie/rules.json")
    assert exists is True and data == {} and err is None
    # empty rules normalize to []
    assert ir.normalize_rules(data) == []


# ---------------------------------------------------------------------------
# glob_ledger
# ---------------------------------------------------------------------------
def test_glob_ledger_unions_and_skips_malformed(tmp_path):
    root = _init_repo(tmp_path)
    # base commit with no changes dir
    _write(root, "seed.txt", "seed")
    _commit(root, "seed")

    _write(root, ".archie/changes/change_1.json",
           {"claims": [{"id": "rule:a", "kind": "rule", "statement": "A"}]})
    _write(root, ".archie/changes/change_2.json",
           {"claims": [{"id": "behavior:b", "kind": "behavior", "statement": "B"},
                       {"id": "rule:a", "kind": "rule", "statement": "A"}]})  # dup id
    (root / ".archie/changes/change_3.json").write_text("{bad json")
    (root / ".archie/changes/latest.json").write_text(
        json.dumps({"claims": [{"id": "z", "kind": "rule", "statement": "Z"}]}))

    claims = ir.glob_ledger(root, "main")
    ids = sorted(c["id"] for c in claims)
    # union of change_1 + change_2, dedup rule:a, malformed skipped, latest.json ignored
    assert ids == ["behavior:b", "rule:a"]


def test_glob_ledger_excludes_records_on_base(tmp_path):
    root = _init_repo(tmp_path)
    _write(root, ".archie/changes/change_1.json",
           {"claims": [{"id": "old", "kind": "rule", "statement": "old"}]})
    _commit(root, "base has change_1")
    _git(root, "checkout", "-q", "-b", "feature")
    _write(root, ".archie/changes/change_2.json",
           {"claims": [{"id": "new", "kind": "rule", "statement": "new"}]})
    _commit(root, "feature adds change_2")

    claims = ir.glob_ledger(root, "main")  # base = main (only has change_1)
    ids = [c["id"] for c in claims]
    assert ids == ["new"]  # change_1 is on base -> excluded


# ---------------------------------------------------------------------------
# parse_event_context
# ---------------------------------------------------------------------------
def test_parse_event_context_ok(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 42,
                                "base": {"ref": "main", "sha": "abc123"}}}))
    ctx = ir.parse_event_context({
        "GITHUB_REPOSITORY": "octo/repo",
        "GITHUB_BASE_REF": "main",
        "GITHUB_EVENT_PATH": str(event),
    })
    assert ctx == ("octo", "repo", 42, "main", "abc123")  # base_sha extracted


def test_parse_event_context_pulls_base_from_payload(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7, "base": {"ref": "develop"}}}))
    ctx = ir.parse_event_context({
        "GITHUB_REPOSITORY": "octo/repo",
        "GITHUB_BASE_REF": "",
        "GITHUB_EVENT_PATH": str(event),
    })
    assert ctx == ("octo", "repo", 7, "develop", "")  # no sha in payload -> ""


def test_parse_event_context_rejects_non_pr(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"push": {}}))
    ctx = ir.parse_event_context({
        "GITHUB_REPOSITORY": "octo/repo",
        "GITHUB_BASE_REF": "main",
        "GITHUB_EVENT_PATH": str(event),
    })
    assert ctx is None
    # malformed repo
    assert ir.parse_event_context({"GITHUB_REPOSITORY": "noslash"}) is None


# ---------------------------------------------------------------------------
# build_changed_items
# ---------------------------------------------------------------------------
def test_build_changed_items_invariant_remove_and_claim():
    base_bp = {"domain_invariants": [
        {"id": "INV1", "invariant": "tenant writes scoped", "keywords": ["tenant"],
         "enforced_at": ["db/payments.py:10"]}]}
    branch_bp = {"domain_invariants": []}  # removed
    claims = [{"kind": "behavior", "statement": "DunningJob calls stripe directly",
               "evidence_files": ["jobs/dunning.py"]}]
    items = ir.build_changed_items(base_bp, branch_bp, [], [], claims)
    inv = [i for i in items if i["section"] == "domain_invariants"]
    assert inv and inv[0]["diff_op"] == "REMOVE" and inv[0]["layer"] == 1
    assert inv[0]["enforced_at_files"] == ["db/payments.py"]
    desc = [i for i in items if i["source"] == "ledger"]
    assert desc and desc[0]["layer"] == 2 and desc[0]["diff_op"] == "DECLARED"
    # every item has a unique ref
    refs = [i["ref"] for i in items]
    assert len(refs) == len(set(refs))


def test_build_changed_items_rule_remove_and_add():
    base_rules = [{"id": "R1", "description": "no direct stripe"}]
    branch_rules = [{"id": "R2", "description": "cap retries at 3"}]
    items = ir.build_changed_items({}, {}, base_rules, branch_rules, [])
    ops = {i["title"]: i["diff_op"] for i in items if i["source"] == "rules"}
    assert ops["R1"] == "REMOVE"
    assert ops["R2"] == "ADD"


# ---------------------------------------------------------------------------
# ledger_join
# ---------------------------------------------------------------------------
def test_ledger_join_matches_on_file_and_keyword():
    item = {"enforced_at_files": ["db/payments.py"], "keywords": ["tenant", "writes"]}
    claims = [{"statement": "tenant writes now unscoped", "evidence_files": ["db/payments.py"],
               "confidence": "low", "reconstructed": True}]
    join = ir.ledger_join(item, claims)
    assert join and join["confidence"] == "low" and join["reconstructed"] is True


def test_ledger_join_no_match_returns_none():
    item = {"enforced_at_files": ["db/payments.py"], "keywords": ["tenant"]}
    # file matches but no keyword overlap
    assert ir.ledger_join(item, [{"statement": "unrelated change",
                                  "evidence_files": ["db/payments.py"],
                                  "confidence": "high"}]) is None
    # keyword matches but no file overlap
    assert ir.ledger_join(item, [{"statement": "tenant logic",
                                  "evidence_files": ["other/file.py"],
                                  "confidence": "high"}]) is None


# ---------------------------------------------------------------------------
# finalize_findings
# ---------------------------------------------------------------------------
def _items():
    return [
        {"ref": "c0", "diff_op": "REMOVE", "layer": 1, "section": "domain_invariants",
         "title": "Tenant isolation", "enforced_at_files": ["db/p.py"], "keywords": ["tenant"]},
        {"ref": "c1", "diff_op": "ADD", "layer": 1, "section": "rules",
         "title": "R2", "enforced_at_files": [], "keywords": ["retry"]},
    ]


def test_finalize_overwrites_and_suppresses():
    model = [
        # valid finding (consolidated shape); model lies about op -> script ignores it
        {"item_refs": ["c0"], "type": "silent_weakening", "change_summary": "removed scoping",
         "colliding_rules": ["der-006"], "because": "rule text says X", "diff_op": "ADD"},
        # because blank -> dropped
        {"item_refs": ["c1"], "type": "contradiction", "change_summary": "R2",
         "colliding_rules": ["x"], "because": "   "},
        # refs don't exist -> dropped
        {"item_refs": ["zzz"], "type": "contradiction", "change_summary": "ghost",
         "colliding_rules": ["y"], "because": "z"},
    ]
    out = ir.finalize_findings(model, _items(), [])
    assert len(out) == 1
    f = out[0]
    assert f["diff_op"] == "REMOVE"            # from c0, not the model's "ADD"
    assert f["change_summary"] == "removed scoping"
    assert f["colliding_rules"] == ["der-006"]
    assert f["layer"] == 1 and f["site_count"] == 1
    assert f["because"] == "rule text says X"


def test_finalize_consolidates_one_change_across_items_and_rules():
    # one change spanning BOTH items, colliding with FOUR rules -> ONE finding
    model = [{"item_refs": ["c0", "c1"], "type": "behavior_violates_rule",
              "change_summary": "cap raised 7->12",
              "colliding_rules": ["inv-002", "der-001", "der-005", "tra-001"],
              "because": "raising the cap unbinds the 7-step constraint"}]
    out = ir.finalize_findings(model, _items(), [])
    assert len(out) == 1
    assert out[0]["site_count"] == 2
    assert out[0]["colliding_rules"] == ["inv-002", "der-001", "der-005", "tra-001"]


def test_dedupe_merges_split_findings_with_same_rule_set():
    # model split the same change into 2 findings hitting the same rule set -> merged
    model = [
        {"item_refs": ["c0"], "type": "behavior_violates_rule", "change_summary": "fn A caps at 12",
         "colliding_rules": ["inv-002", "der-001"], "because": "A violates the cap"},
        {"item_refs": ["c1"], "type": "behavior_violates_rule", "change_summary": "fn B caps at 12",
         "colliding_rules": ["der-001", "inv-002"], "because": "B violates the cap"},
    ]
    out = ir.finalize_findings(model, _items(), [])
    assert len(out) == 1 and out[0]["site_count"] == 2


def test_finalize_attaches_ledger_confidence():
    items = _items()
    claims = [{"statement": "tenant scoping dropped", "evidence_files": ["db/p.py"],
               "confidence": "low", "reconstructed": True}]
    model = [{"item_refs": ["c0"], "type": "silent_weakening", "change_summary": "removed",
              "colliding_rules": ["der-006"], "because": "cited"}]
    out = ir.finalize_findings(model, items, claims)
    assert out[0]["confidence"] == "low" and out[0]["reconstructed"] is True


# ---------------------------------------------------------------------------
# render_comment
# ---------------------------------------------------------------------------
def test_render_comment_no_diff_returns_none():
    assert ir.render_comment([], False, (True, [])) is None


def test_render_comment_no_findings_is_consistent_message():
    body = ir.render_comment([], True, (True, []))
    assert ir.COMMENT_MARKER in body
    assert "consistent" in body.lower()


def test_render_comment_groups_and_cites():
    findings = [
        {"type": "silent_weakening", "diff_op": "REMOVE", "layer": 1, "site_count": 1,
         "change_summary": "Tenant isolation dropped", "colliding_rules": ["der-002"],
         "because": "invariant text required tenant_id", "confidence": "low",
         "reconstructed": True},
        {"type": "behavior_violates_rule", "diff_op": "DECLARED", "layer": 2, "site_count": 2,
         "change_summary": "cap raised 7->12", "colliding_rules": ["inv-002", "der-001"],
         "because": "R2 forbids it", "confidence": None},
    ]
    body = ir.render_comment(findings, True, (True, []))
    assert ir.COMMENT_MARKER in body
    assert "Silent weakening" in body and "Behavior may violate" in body
    assert "Because:" in body
    assert "ledger confidence: low" in body
    assert "Collides with: **inv-002, der-001**" in body   # rules listed in ONE finding
    assert "2 sites" in body                                 # consolidated across sites
    assert "reconstructed guess" in body
    assert "doesn't block" in body


# ---------------------------------------------------------------------------
# model + github calls (monkeypatched urllib)
# ---------------------------------------------------------------------------
def test_call_anthropic_delegates_to_llm_client(monkeypatch):
    seen = {}

    def fake_complete(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return {"text": "", "tool_calls": [
            {"name": "emit_findings", "input": {"findings": [{"id": 1}]}}]}

    monkeypatch.setattr(ir.llm_client, "complete", fake_complete)
    out = ir.call_anthropic("SYS", "USER", "sk-key", max_retries=2)
    assert out == [{"id": 1}]
    assert seen["prompt"] == "USER"
    assert seen["system"] == "SYS"
    assert seen["tool_choice"] == "emit_findings"
    assert seen["tools"] == [ir.EMIT_FINDINGS_TOOL]
    assert seen["max_retries"] == 2
    assert seen["tier"] == "haiku"


def test_call_anthropic_wraps_llmerror(monkeypatch):
    def boom(prompt, **kw):
        raise ir.llm_client.LLMError("HTTP 500")

    monkeypatch.setattr(ir.llm_client, "complete", boom)
    with pytest.raises(RuntimeError):
        ir.call_anthropic("s", "u", "sk-key")


def test_post_or_update_comment_creates_then_updates(monkeypatch):
    calls = []

    def fake_gh(method, url, token, body=None):
        calls.append((method, url, body))
        if method == "GET":
            # first GET: no existing comment; later GET: existing with marker.
            # _gh_request now returns (data, link_header).
            if len([c for c in calls if c[0] == "POST"]) == 0:
                return [], None
            return [{"id": 99, "body": ir.COMMENT_MARKER + "\nold"}], None
        return {}, None

    monkeypatch.setattr(ir, "_gh_request", fake_gh)

    ir.post_or_update_comment("o", "r", 1, ir.COMMENT_MARKER + "\nnew", "tok")
    assert calls[-1][0] == "POST"  # created

    ir.post_or_update_comment("o", "r", 1, ir.COMMENT_MARKER + "\nnewer", "tok")
    assert calls[-1][0] == "PATCH"  # updated existing id 99
    assert "/comments/99" in calls[-1][1]


def test_next_link_parses_pagination():
    hdr = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=5>; rel="last"'
    assert ir._next_link(hdr) == "https://api.github.com/x?page=2"
    assert ir._next_link("") is None


def test_find_existing_comment_follows_pagination(monkeypatch):
    # marker only on page 2 -> must follow the Link header, not duplicate-POST
    pages = {
        "url1": ([{"id": 1, "body": "noise"}], "<url2>; rel=\"next\""),
        "url2": ([{"id": 2, "body": ir.COMMENT_MARKER}], None),
    }
    seq = ["url1", "url2"]

    def fake_gh(method, url, token, body=None):
        return pages[seq.pop(0)]

    monkeypatch.setattr(ir, "_gh_request", fake_gh)
    assert ir._find_existing_comment_id("o", "r", 1, "tok") == 2


def test_safe_post_comment_swallows_urlerror(monkeypatch):
    def boom(*a, **k):
        raise ir.urllib.error.URLError("network down")

    monkeypatch.setattr(ir, "post_or_update_comment", boom)
    # must NOT raise (never block)
    ir.safe_post_comment("o", "r", 1, "body", "tok")


def test_safe_post_comment_skips_without_token():
    # no token -> no attempt, no raise
    ir.safe_post_comment("o", "r", 1, "body", "")


# ---------------------------------------------------------------------------
# item_key fallback + newly-diffed sections
# ---------------------------------------------------------------------------
def test_item_key_fallback_avoids_collision():
    a = {"note": "x"}   # no id, no title
    b = {"note": "y"}
    ka, kb = ir.item_key(a, None, "title"), ir.item_key(b, None, "title")
    assert ka != kb and ka.startswith("item_")


def test_pitfalls_remove_is_layer1():
    base_bp = {"pitfalls": [{"id": "pf1", "problem_statement": "don't double-migrate"}]}
    items = ir.build_changed_items(base_bp, {"pitfalls": []}, [], [], [])
    pf = [i for i in items if i["section"] == "pitfalls"]
    assert pf and pf[0]["diff_op"] == "REMOVE" and pf[0]["layer"] == 1


def test_trade_offs_and_out_of_scope_diffed():
    base_bp = {"decisions": {
        "trade_offs": [{"title": "latency for consistency"}],
        "out_of_scope": [{"title": "no multi-region"}],
    }}
    branch_bp = {"decisions": {"trade_offs": [], "out_of_scope": [{"title": "no multi-region"}]}}
    items = ir.build_changed_items(base_bp, branch_bp, [], [], [])
    sections = {i["section"]: i["diff_op"] for i in items}
    assert sections.get("decisions.trade_offs") == "REMOVE"
    assert "decisions.out_of_scope" not in sections  # unchanged -> no diff


def test_unenforced_invariants_not_diffed():
    base_bp = {"unenforced_invariants": [{"id": "u1", "invariant": "advisory gap"}]}
    items = ir.build_changed_items(base_bp, {"unenforced_invariants": []}, [], [], [])
    assert not [i for i in items if i["section"] == "unenforced_invariants"]


def test_data_model_pure_add_is_surfaced():
    # pure ADD of a data model is now surfaced (script owns WHAT changed; model judges)
    items = ir.build_changed_items({}, {"data_models": [{"name": "Invoice"}]}, [], [], [])
    dm = [i for i in items if i["section"] == "data_models"]
    assert dm and dm[0]["diff_op"] == "ADD" and dm[0]["layer"] == 2


def test_component_remove_is_diffed_layer2():
    # a component REMOVE is caught (keyed by name, Layer 2) — coverage gap fix
    base_bp = {"components": [{"name": "PaymentGateway", "responsibility": "money"}]}
    items = ir.build_changed_items(base_bp, {"components": []}, [], [], [])
    c = [i for i in items if i["section"] == "components"]
    assert c and c[0]["diff_op"] == "REMOVE" and c[0]["layer"] == 2


def test_rule_remove_is_layer1_branch_none():
    items = ir.build_changed_items({}, {}, [{"id": "R1", "description": "x"}], [], [])
    r = [i for i in items if i["source"] == "rules"][0]
    assert r["diff_op"] == "REMOVE" and r["layer"] == 1 and r["branch_item"] is None


# ---------------------------------------------------------------------------
# retained_rules + _path_overlap
# ---------------------------------------------------------------------------
def test_retained_rules_under_threshold_returns_all():
    rules = [{"id": f"R{i}", "description": "d"} for i in range(3)]
    # none changed -> all retained, count under threshold -> returned as-is
    assert ir.retained_rules(rules, []) == rules


def test_retained_rules_excludes_changed():
    rules = [{"id": "R1", "description": "a"}, {"id": "R2", "description": "b"}]
    changed = [{"source": "rules", "title": "R1", "keywords": []}]
    out = ir.retained_rules(rules, changed)
    assert [ir._rule_title(r) for r in out] == ["R2"]


def test_path_overlap_cases():
    assert ir._path_overlap({"db/p.py"}, {"db/p.py"}) is True       # exact
    assert ir._path_overlap({"src/db/p.py"}, {"db/p.py"}) is True   # suffix
    assert ir._path_overlap({"a/x.py"}, {"b/y.py"}) is False        # disjoint


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# render flag order
# ---------------------------------------------------------------------------
def test_render_comment_preserves_flag_order():
    findings = [
        {"type": "behavior_violates_rule", "diff_op": "DECLARED", "layer": 2, "site_count": 1,
         "change_summary": "B", "colliding_rules": [], "because": "b"},
        {"type": "silent_weakening", "diff_op": "REMOVE", "layer": 1, "site_count": 1,
         "change_summary": "A", "colliding_rules": [], "because": "a"},
    ]
    body = ir.render_comment(findings, True, (True, []))
    assert body.index("Silent weakening") < body.index("Behavior may violate")


# ---------------------------------------------------------------------------
# full main() integration via a real origin clone (the base-ref diff path)
# ---------------------------------------------------------------------------
def test_main_flags_removed_invariant_via_origin(tmp_path, monkeypatch):
    # upstream repo (origin) on main holds the invariant
    up = tmp_path / "up"
    up.mkdir()
    _init_repo(up)
    _write(up, ".archie/blueprint.json", {"domain_invariants": [
        {"id": "INV1", "invariant": "tenant writes scoped",
         "keywords": ["tenant"], "enforced_at": ["db/p.py:1"]}]})
    _write(up, ".archie/rules.json", {"rules": []})
    _commit(up, "base")
    base_sha = _git(up, "rev-parse", "HEAD")

    # working clone gets origin/main
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(up), str(work)], check=True)
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "T")
    _git(work, "checkout", "-q", "-b", "feature")
    _write(work, ".archie/blueprint.json", {"domain_invariants": []})  # removed
    _write(work, ".archie/changes/change_1.json", {"claims": [
        {"id": "d", "kind": "behavior", "statement": "tenant scoping removed",
         "evidence_files": ["db/p.py"], "confidence": "low", "reconstructed": True}]})
    _commit(work, "remove invariant")

    # Drive the diff off the base SHA (the robust path), not origin/<base>.
    event = work / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 5,
                                "base": {"ref": "main", "sha": base_sha}}}))

    captured = {}
    monkeypatch.setattr(ir, "call_anthropic", lambda s, u, k, **kw: [
        {"item_refs": ["c0"], "type": "silent_weakening", "change_summary": "tenant scoping removed",
         "colliding_rules": ["der-002"], "because": "base invariant required tenant_id scoping"}])
    monkeypatch.setattr(ir, "safe_post_comment",
                        lambda o, r, n, body, t: captured.update(body=body))
    for k, v in {"GITHUB_WORKSPACE": str(work), "ANTHROPIC_API_KEY": "sk-x",
                 "GITHUB_REPOSITORY": "o/r", "GITHUB_BASE_REF": "main",
                 "GITHUB_EVENT_PATH": str(event), "GITHUB_TOKEN": "tok"}.items():
        monkeypatch.setenv(k, v)

    rc = ir.main()
    assert rc == 0
    assert "Silent weakening" in captured["body"]
    assert "ledger confidence: low" in captured["body"]  # the conservative join attached it


def test_main_skips_without_secret(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(ir.llm_client, "resolve_config", lambda *a, **kw: None)
    assert ir.main() == 0  # fork PR / no secret -> never block


# ---------------------------------------------------------------------------
# sync advisory — "was an /archie-sync made for this PR?"
# ---------------------------------------------------------------------------
def _commit_sha(root: Path, msg: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def test_sync_advisory_synced_then_drift(tmp_path):
    import sync  # noqa: E402  (same _STANDALONE path)
    root = _init_repo(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "A.kt").write_text("fun a()=1\n")
    base = _commit_sha(root, "base")
    (root / "src" / "A.kt").write_text("fun a()=2\n")
    (root / "src" / "B.kt").write_text("fun b()=3\n")
    _commit_sha(root, "work")

    # Stamp covering the current code, then the advisory sees a fully-synced PR.
    sync.cmd_sync_stamp(root)
    _commit_sha(root, "stamp")
    present, unsynced = ir.sync_advisory(root, base)
    assert present and unsynced == []

    # Drift ONE file without re-stamping — only that file is flagged (content-based).
    (root / "src" / "A.kt").write_text("fun a()=99\n")
    _commit_sha(root, "drift")
    present, unsynced = ir.sync_advisory(root, base)
    assert present and unsynced == ["src/A.kt"]

    # No marker at all -> every changed source file is flagged, present=False.
    (root / ".archie" / "sync_state.json").unlink()
    present, unsynced = ir.sync_advisory(root, base)
    assert not present and set(unsynced) == {"src/A.kt", "src/B.kt"}


def test_sync_advisory_excludes_ignored_and_skipdir(tmp_path):
    """Regression: the check must use the SAME file universe as the stamp. A
    tracked source file under a SKIP_DIRS dir (vendor/) or a gitignored dir is
    excluded from the stamp, so it must NOT be flagged unsynced after a sync —
    otherwise it would fire on every PR forever, unfixable."""
    import sync  # noqa: E402
    root = _init_repo(tmp_path)
    for d in ("src", "vendor", "generated"):
        (root / d).mkdir()
    (root / ".gitignore").write_text("generated/\n")
    (root / "src" / "A.kt").write_text("fun a()=1\n")
    (root / "vendor" / "lib.go").write_text("package v\n")     # under SKIP_DIRS
    (root / "generated" / "Gen.kt").write_text("val g=1\n")    # under a gitignored dir
    _git(root, "add", "-f", "generated/Gen.kt")
    base = _commit_sha(root, "base")
    (root / "src" / "A.kt").write_text("fun a()=2\n")
    (root / "vendor" / "lib.go").write_text("package v2\n")
    (root / "generated" / "Gen.kt").write_text("val g=2\n")
    _commit_sha(root, "work")
    sync.cmd_sync_stamp(root)
    present, unsynced = ir.sync_advisory(root, base)
    assert unsynced == []  # vendor/ + generated/ excluded both sides; src/A.kt is covered


def test_sync_advisory_skips_deletions(tmp_path):
    """Regression: a deleted source file must NOT be reported as a 'changed,
    please re-sync' phantom pointing at a path that no longer exists."""
    import sync  # noqa: E402
    root = _init_repo(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "A.kt").write_text("fun a()=1\n")
    (root / "src" / "B.kt").write_text("fun b()=1\n")
    base = _commit_sha(root, "base")
    sync.cmd_sync_stamp(root)
    _commit_sha(root, "stamp")
    (root / "src" / "B.kt").unlink()
    _commit_sha(root, "delete B")
    present, unsynced = ir.sync_advisory(root, base)
    assert "src/B.kt" not in unsynced


def test_sync_section_and_render():
    assert ir._sync_section((True, [])) == ""               # nothing to flag
    s = ir._sync_section((True, ["src/A.kt"]))
    assert "out of sync" in s and "src/A.kt" in s and "does not block" in s
    assert "No `/archie-sync` recorded" in ir._sync_section((False, ["x.kt"]))
    # render posts a body for a sync-only advisory (no blueprint diff)...
    body = ir.render_comment([], False, (True, ["src/A.kt"]))
    assert body is not None and "out of sync" in body
    # ...and nothing when there's neither an intent review nor an advisory.
    assert ir.render_comment([], False, (True, [])) is None


def test_render_comment_model_failed_is_explicit():
    """Regression (#6): a model failure on a real blueprint diff must NOT
    masquerade as a clean review — it renders an explicit 'could not run' notice."""
    body = ir.render_comment([], True, (True, []), model_failed=True)
    assert body is not None
    assert "could not run" in body.lower() and "manually" in body.lower()


def test_sync_advisory_handles_non_ascii_paths(tmp_path):
    """Regression: a drifted source file with a non-ASCII name must still be flagged.
    Git quotes such paths by default (core.quotePath); the -z diff recovers the raw
    path so it isn't silently dropped."""
    import sync  # noqa: E402
    root = _init_repo(tmp_path)
    _git(root, "config", "core.quotePath", "true")
    (root / "src").mkdir()
    (root / "src" / "café.kt").write_text("fun c()=1\n")
    base = _commit_sha(root, "base")
    sync.cmd_sync_stamp(root)
    _commit_sha(root, "stamp")
    (root / "src" / "café.kt").write_text("fun c()=99\n")
    _commit_sha(root, "drift")
    present, unsynced = ir.sync_advisory(root, base)
    # normalization-robust: exactly one flagged, a .kt under src/ (the café file)
    assert len(unsynced) == 1
    assert unsynced[0].startswith("src/") and unsynced[0].endswith(".kt")


def test_main_posts_sync_advisory_without_blueprint(tmp_path, monkeypatch):
    """Regression (#7): the advisory must surface even when the branch has NO
    blueprint — the old code returned before computing it."""
    import sync  # noqa: E402
    up = tmp_path / "up"
    up.mkdir()
    _init_repo(up)
    (up / "src").mkdir()
    (up / "src" / "A.kt").write_text("fun a()=1\n")
    sync.cmd_sync_stamp(up)                 # stamp records A.kt's base hash (no blueprint)
    _commit(up, "base")
    base_sha = _git(up, "rev-parse", "HEAD")

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(up), str(work)], check=True)
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "T")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "src" / "A.kt").write_text("fun a()=2\n")   # drift, still no blueprint anywhere
    _commit(work, "drift")

    event = work / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7,
                                "base": {"ref": "main", "sha": base_sha}}}))
    captured = {}
    monkeypatch.setattr(ir, "safe_post_comment",
                        lambda o, r, n, body, t: captured.update(body=body))
    for k, v in {"GITHUB_WORKSPACE": str(work), "ANTHROPIC_API_KEY": "sk-x",
                 "GITHUB_REPOSITORY": "o/r", "GITHUB_BASE_REF": "main",
                 "GITHUB_EVENT_PATH": str(event), "GITHUB_TOKEN": "tok"}.items():
        monkeypatch.setenv(k, v)

    assert ir.main() == 0
    assert "out of sync" in captured.get("body", "")
