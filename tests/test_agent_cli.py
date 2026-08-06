"""Tests for `agent_cli` — the runtime per-CLI adapter.

agent_cli is the single home for headless coding-agent CLI invocation: harness
detection plus the actual `claude` / `codex` shell-outs. Pipeline scripts
(verify_findings.py) call `detect_verifier` / `run_verifier` and stay
CLI-agnostic; all per-CLI knowledge is concentrated here.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


_STANDALONE = Path(__file__).resolve().parent.parent / "archie" / "standalone"
sys.path.insert(0, str(_STANDALONE))

import agent_cli  # noqa: E402
import llm_client  # noqa: E402


def _no_clis(monkeypatch):
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)


class TestRunVerifierApiPath:
    def test_api_path_uses_llm_client(self, monkeypatch, tmp_path):
        _no_clis(monkeypatch)
        seen = {}
        def fake_complete(prompt, **kw):
            seen.update(kw, prompt=prompt)
            return {"text": "verdict", "tool_calls": []}
        monkeypatch.setattr(agent_cli.llm_client, "resolve_config",
                            lambda project_root=None, env=None: {"backend": "openai"})
        monkeypatch.setattr(agent_cli.llm_client, "complete", fake_complete)
        out = agent_cli.run_verifier("p", tmp_path, "claude", model="sonnet")
        assert out == "verdict"
        assert seen["tier"] == "sonnet"
        assert seen["prompt"] == "p"

    def test_tools_flag_passes_executor(self, monkeypatch, tmp_path):
        _no_clis(monkeypatch)
        seen = {}
        monkeypatch.setattr(agent_cli.llm_client, "resolve_config",
                            lambda project_root=None, env=None: {"backend": "openai"})
        monkeypatch.setattr(agent_cli.llm_client, "complete",
                            lambda prompt, **kw: seen.update(kw) or
                            {"text": "t", "tool_calls": []})
        agent_cli.run_verifier("p", tmp_path, "claude", tools=True)
        assert callable(seen["tool_executor"])
        assert seen["tools"] == agent_cli._TOOLS

    def test_no_provider_returns_empty(self, monkeypatch, tmp_path):
        _no_clis(monkeypatch)
        monkeypatch.setattr(agent_cli.llm_client, "resolve_config",
                            lambda project_root=None, env=None: None)
        assert agent_cli.run_verifier("p", tmp_path, "claude") == ""

    def test_llm_error_fails_open(self, monkeypatch, tmp_path):
        _no_clis(monkeypatch)
        def boom(prompt, **kw):
            raise llm_client.LLMError("down")
        monkeypatch.setattr(agent_cli.llm_client, "resolve_config",
                            lambda project_root=None, env=None: {"backend": "openai"})
        monkeypatch.setattr(agent_cli.llm_client, "complete", boom)
        assert agent_cli.run_verifier("p", tmp_path, "claude") == ""


def test_grep_does_not_leak_symlinked_file_outside_repo(tmp_path):
    # A symlinked FILE committed inside the checkout that points outside the
    # repo must not be read by grep — rglob yields it and relative_to() would
    # relabel it as an in-repo path, leaking an outside secret into the review.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "clean.py").write_text("harmless = 1\n")
    outside = tmp_path / "outside_secret.py"
    outside.write_text("API_KEY = 'supersecret-leak'\n")
    link = repo / "innocent.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")

    out = agent_cli._exec_tool(repo, "grep", {"pattern": "supersecret", "glob": "*.py"})
    assert "supersecret" not in out          # the outside file was NOT read
    assert "innocent.py" not in out          # nor relabelled as an in-repo hit
    # a real in-repo match still works
    assert "harmless" in agent_cli._exec_tool(repo, "grep", {"pattern": "harmless", "glob": "*.py"})


# ---------------------------------------------------------------------------
# detect_verifier — picks the harness from the environment
# ---------------------------------------------------------------------------

def test_detect_verifier_claude_when_claudecode_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert agent_cli.detect_verifier() == "claude"


def test_detect_verifier_codex_when_not_claude_and_codex_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which",
                        lambda name: "/usr/bin/codex" if name == "codex" else None)
    assert agent_cli.detect_verifier() == "codex"


def test_detect_verifier_falls_back_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLAUDECODE and no codex on PATH → fall back to claude (which itself
    no-ops gracefully if that CLI is also absent)."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)
    assert agent_cli.detect_verifier() == "claude"


# ---------------------------------------------------------------------------
# detect_cli — harness identity for telemetry attribution
# ---------------------------------------------------------------------------

def test_detect_cli_claude_when_claudecode_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert agent_cli.detect_cli() == "claude"


def test_detect_cli_codex_when_not_claude_and_codex_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which",
                        lambda name: "/usr/bin/codex" if name == "codex" else None)
    assert agent_cli.detect_cli() == "codex"


def test_detect_cli_unknown_when_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike detect_verifier, telemetry detection reports "unknown" rather than
    guessing — an honest value beats a wrong one in the analytics."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)
    assert agent_cli.detect_cli() == "unknown"


def test_detect_verifier_shares_detection_with_detect_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """detect_verifier must agree with detect_cli except for the indeterminate
    case, where it falls back to claude so the verifier always has a CLI."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: None)
    assert agent_cli.detect_cli() == "unknown"
    assert agent_cli.detect_verifier() == "claude"


# ---------------------------------------------------------------------------
# run_verifier — dispatch to the right CLI
# ---------------------------------------------------------------------------

def test_run_verifier_dispatches_to_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """verifier='codex' must route through the Codex path, not the Claude one."""
    calls: list[str] = []
    seen_model: dict[str, str] = {}
    monkeypatch.setattr(
        agent_cli, "_run_codex",
        lambda prompt, root, timeout: calls.append("codex") or "codex-out",
    )
    monkeypatch.setattr(
        agent_cli, "_run_claude",
        lambda prompt, root, timeout, model="haiku": (calls.append("claude"),
                                                      seen_model.__setitem__("m", model), "claude-out")[-1],
    )
    # Make both CLIs appear available on PATH so run_verifier routes by verifier arg.
    monkeypatch.setattr(
        agent_cli.shutil, "which",
        lambda name: f"/usr/local/bin/{name}" if name in ("codex", "claude") else None,
    )

    assert agent_cli.run_verifier("p", tmp_path, "codex") == "codex-out"
    assert agent_cli.run_verifier("p", tmp_path, "claude") == "claude-out"
    assert calls == ["codex", "claude"]
    # model alias must thread through run_verifier -> _run_claude (§6.6a tracer/challenger).
    assert agent_cli.run_verifier("p", tmp_path, "claude", model="opus") == "claude-out"
    assert seen_model["m"] == "opus"


# ---------------------------------------------------------------------------
# _run_codex — invocation shape
# ---------------------------------------------------------------------------

def test_run_codex_uses_stdin_and_output_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake_run(args, input, capture_output, text, cwd, timeout, env=None):
        seen["args"] = args
        seen["input"] = input
        seen["cwd"] = cwd
        seen["env"] = env
        out_idx = args.index("--output-last-message") + 1
        Path(args[out_idx]).write_text('{"id":"f","verdict":"keep","confidence":1.0,"reason":"ok"}')
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(agent_cli.subprocess, "run", _fake_run)
    result = agent_cli._run_codex("PROMPT BODY", tmp_path, 90)

    assert '"verdict":"keep"' in result
    assert seen["input"] == "PROMPT BODY"
    assert seen["cwd"] == str(tmp_path)
    assert seen["args"][-1] == "-"
    assert "--output-last-message" in seen["args"]
    # Internal spawn must carry the marker so it can't pollute the intent log.
    assert seen["env"] is not None and seen["env"].get("ARCHIE_INTERNAL") == "1"


def test_codex_cleans_temp_on_nonzero_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Temp file must be deleted even when codex exits with a non-zero returncode."""
    created_tmp: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def _tracking_mkstemp(**kwargs):
        fd, name = real_mkstemp(**kwargs)
        created_tmp.append(name)
        return fd, name

    # mkstemp is called as tempfile.mkstemp inside agent_cli, so patch the module
    monkeypatch.setattr(agent_cli.tempfile, "mkstemp", _tracking_mkstemp)

    def _fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(agent_cli.subprocess, "run", _fake_run)

    result = agent_cli._run_codex("prompt", tmp_path, 30)

    assert result == ""
    assert len(created_tmp) == 1, "expected exactly one temp file to be created"
    assert not Path(created_tmp[0]).exists(), "temp file was NOT cleaned up on non-zero returncode"


def test_codex_cleans_temp_on_missing_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Temp file must be deleted when the codex CLI is not found (FileNotFoundError)."""
    created_tmp: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def _tracking_mkstemp(**kwargs):
        fd, name = real_mkstemp(**kwargs)
        created_tmp.append(name)
        return fd, name

    monkeypatch.setattr(agent_cli.tempfile, "mkstemp", _tracking_mkstemp)

    def _fake_run(args, **kwargs):
        raise FileNotFoundError("codex not found")

    monkeypatch.setattr(agent_cli.subprocess, "run", _fake_run)

    result = agent_cli._run_codex("prompt", tmp_path, 30)

    assert result == ""
    assert len(created_tmp) == 1, "expected exactly one temp file to be created"
    assert not Path(created_tmp[0]).exists(), "temp file was NOT cleaned up on FileNotFoundError"


# ---------------------------------------------------------------------------
# _exec_tool jail tests — path-escape and .git access denial
# ---------------------------------------------------------------------------

class TestExecToolJail:
    """Direct _exec_tool jail tests without LLM transport."""

    def test_read_file_path_escape_denied(self, tmp_path: Path) -> None:
        """read_file with path-escape attempt (../) must be denied."""
        # Create a file outside tmp_path that actually exists
        (tmp_path / "inside.txt").write_text("inside\n")
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret\n")

        result = agent_cli._exec_tool(
            tmp_path, "read_file", {"path": "../outside.txt"}
        )
        assert result.startswith("denied"), \
            f"expected 'denied', got: {result}"
        assert "secret" not in result, "outside file was leaked"

    def test_read_file_git_path_denied(self, tmp_path: Path) -> None:
        """read_file targeting .git/config must be denied."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        git_config = git_dir / "config"
        git_config.write_text("[core]\n  bare = false\n")

        result = agent_cli._exec_tool(tmp_path, "read_file", {"path": ".git/config"})
        assert result.startswith("denied"), \
            f"expected 'denied', got: {result}"

    def test_read_file_happy_path(self, tmp_path: Path) -> None:
        """read_file happy path: create file, read it, get numbered lines."""
        test_file = tmp_path / "f.py"
        test_file.write_text("line one\nline two\n")

        result = agent_cli._exec_tool(tmp_path, "read_file", {"path": "f.py"})
        assert "1: line one" in result
        assert "2: line two" in result
        assert not result.startswith("denied")

    def test_read_file_with_line_range(self, tmp_path: Path) -> None:
        """read_file with start_line/end_line range."""
        test_file = tmp_path / "f.py"
        test_file.write_text("a\nb\nc\nd\ne\n")

        result = agent_cli._exec_tool(
            tmp_path, "read_file", {"path": "f.py", "start_line": 2, "end_line": 4}
        )
        assert "2: b" in result
        assert "3: c" in result
        assert "4: d" in result
        assert "1: a" not in result
        assert "5: e" not in result

    def test_read_file_nonexistent(self, tmp_path: Path) -> None:
        """read_file on nonexistent file must be denied."""
        result = agent_cli._exec_tool(
            tmp_path, "read_file", {"path": "nonexistent.txt"}
        )
        assert result.startswith("denied")

    def test_grep_happy_path(self, tmp_path: Path) -> None:
        """grep happy path: search pattern in files."""
        (tmp_path / "a.py").write_text("def hello():\n    print('hi')\n")
        (tmp_path / "b.py").write_text("def world():\n    pass\n")

        result = agent_cli._exec_tool(tmp_path, "grep", {"pattern": "def", "glob": "*.py"})
        assert "def hello" in result
        assert "def world" in result
        assert not result.startswith("denied")

    def test_grep_no_matches(self, tmp_path: Path) -> None:
        """grep with no matches returns 'no matches'."""
        (tmp_path / "a.py").write_text("hello\n")

        result = agent_cli._exec_tool(tmp_path, "grep", {"pattern": "xyz", "glob": "*.py"})
        assert result == "no matches"

    def test_grep_bad_pattern(self, tmp_path: Path) -> None:
        """grep with invalid regex pattern is denied."""
        (tmp_path / "a.py").write_text("test\n")

        result = agent_cli._exec_tool(tmp_path, "grep", {"pattern": "[invalid(regex"})
        assert result.startswith("denied")

    def test_unknown_tool_denied(self, tmp_path: Path) -> None:
        """Unknown tool name returns denied message."""
        result = agent_cli._exec_tool(tmp_path, "unknown_tool", {})
        assert result == "denied: unknown tool"


# ---------------------------------------------------------------------------
# End-to-end tool loop through run_verifier with tools=True
# ---------------------------------------------------------------------------

class TestEndToEndToolLoop:
    """Test tool loop integration when run_verifier(tools=True)."""

    def test_run_verifier_with_tools_executes_jailed_tool(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_verifier(tools=True) wires _exec_tool into llm_client for tool calls."""
        _no_clis(monkeypatch)

        # Create a file we'll try to escape from
        (tmp_path / "safe.txt").write_text("safe content\n")
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("API_KEY='supersecret'\n")

        # Track what tool_executor receives and returns
        seen_tool_calls = []

        def fake_complete(prompt, **kw):
            """Simulate LLM calling the tool with a path-escape attempt."""
            tool_executor = kw.get("tool_executor")
            if tool_executor:
                # Try to escape the jail
                result = tool_executor("read_file", {"path": "../secret.txt"})
                seen_tool_calls.append(("read_file", {"path": "../secret.txt"}, result))
            return {"text": "tool result", "tool_calls": []}

        monkeypatch.setattr(agent_cli.llm_client, "resolve_config",
                            lambda project_root=None, env=None: {"backend": "openai"})
        monkeypatch.setattr(agent_cli.llm_client, "complete", fake_complete)

        result = agent_cli.run_verifier("prompt", tmp_path, "claude", tools=True)

        # Verify the tool executor was called and jailed the path-escape
        assert len(seen_tool_calls) == 1
        tool_name, tool_args, tool_result = seen_tool_calls[0]
        assert tool_name == "read_file"
        assert "denied" in tool_result, \
            f"expected 'denied' in jail result, got: {tool_result}"
        assert "supersecret" not in tool_result


# ---------------------------------------------------------------------------
# Dispatch priority tests — CLI precedence
# ---------------------------------------------------------------------------

class TestDispatchPriority:
    """Test run_verifier dispatch order: codex → claude → API → ""."""

    def test_dispatch_prefers_codex_when_requested_and_available(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """verifier='codex' must dispatch to _run_codex, not API."""
        # Mock codex on PATH
        monkeypatch.setattr(
            agent_cli.shutil, "which",
            lambda name: "/usr/bin/codex" if name == "codex" else None,
        )

        # Track which path was taken
        calls = []

        def fake_run_codex(prompt, root, timeout):
            calls.append("codex")
            return "codex-output"

        def fake_complete(prompt, **kw):
            calls.append("api")
            raise AssertionError("API must not be called when codex is available")

        monkeypatch.setattr(agent_cli, "_run_codex", fake_run_codex)
        monkeypatch.setattr(agent_cli.llm_client, "complete", fake_complete)

        result = agent_cli.run_verifier("prompt", tmp_path, "codex")

        assert result == "codex-output"
        assert calls == ["codex"], \
            f"expected only ['codex'] call path, got: {calls}"

    def test_dispatch_falls_back_from_codex_to_claude(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When verifier='codex' but codex not on PATH, should try claude."""
        # No codex on PATH; claude is available
        monkeypatch.setattr(
            agent_cli.shutil, "which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )

        calls = []

        def fake_run_claude(prompt, root, timeout, model="haiku"):
            calls.append("claude")
            return "claude-output"

        monkeypatch.setattr(agent_cli, "_run_claude", fake_run_claude)

        result = agent_cli.run_verifier("prompt", tmp_path, "codex")

        assert result == "claude-output"
        assert "claude" in calls

    def test_dispatch_prefers_claude_when_available(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """claude on PATH must be preferred over API."""
        # Claude available, API config disabled
        monkeypatch.setattr(
            agent_cli.shutil, "which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )

        calls = []

        def fake_run_claude(prompt, root, timeout, model="haiku"):
            calls.append("claude")
            return "claude-output"

        def fake_complete(prompt, **kw):
            calls.append("api")
            raise AssertionError("API must not be called when claude is available")

        monkeypatch.setattr(agent_cli, "_run_claude", fake_run_claude)
        monkeypatch.setattr(agent_cli.llm_client, "complete", fake_complete)

        result = agent_cli.run_verifier("prompt", tmp_path, "claude")

        assert result == "claude-output"
        assert calls == ["claude"]

    def test_dispatch_falls_back_to_api_when_no_cli(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no CLI available, must use LLM API."""
        # No CLI on PATH
        monkeypatch.setattr(
            agent_cli.shutil, "which",
            lambda name: None,
        )

        calls = []

        def fake_complete(prompt, **kw):
            calls.append("api")
            return {"text": "api-output", "tool_calls": []}

        monkeypatch.setattr(agent_cli.llm_client, "resolve_config",
                            lambda project_root=None, env=None: {"backend": "openai"})
        monkeypatch.setattr(agent_cli.llm_client, "complete", fake_complete)

        result = agent_cli.run_verifier("prompt", tmp_path, "claude")

        assert result == "api-output"
        assert calls == ["api"]

    def test_dispatch_returns_empty_when_nothing_available(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no CLI and no API config, return empty string."""
        _no_clis(monkeypatch)
        monkeypatch.setattr(agent_cli.llm_client, "resolve_config",
                            lambda project_root=None, env=None: None)

        result = agent_cli.run_verifier("prompt", tmp_path, "claude")
        assert result == ""
