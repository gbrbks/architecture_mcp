#!/usr/bin/env python3
"""Runtime per-CLI adapter — headless invocation of the user's coding-agent CLI.

Archie's standalone pipeline scripts are CLI-agnostic. A few of them, though,
must spawn an AI model mid-pipeline (the finding backward-check). They cannot
reach the connector layer — connectors are install-time objects, gone by the
time a scan runs. This module is the runtime counterpart:

    connector  = install-time per-CLI adapter (translates the manifest, renders
                 the workflow into per-CLI install artifacts)
    agent_cli  = runtime per-CLI adapter (turns "run this prompt through the
                 user's coding agent" into the right headless CLI call)

All per-CLI invocation knowledge lives here, in ONE place. A pipeline script
that needs an AI call imports `detect_verifier` / `run_verifier` and stays
fully CLI-agnostic itself.

Like the rest of `archie/standalone/`, this shells out to a CLI rather than
importing a vendor SDK — keeps the zero-dependency invariant and inherits the
user's existing auth. When no coding-agent CLI is available (e.g. CI), it
falls back to a direct LLM API call via `llm_client`, which resolves any
OpenAI-compatible endpoint (OpenRouter, Anthropic, etc.) from project/env
config.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_client  # noqa: E402

CLAUDE_CLI = "claude"
CODEX_CLI = "codex"
DEFAULT_TIMEOUT = 90  # seconds


def _run_api(prompt: str, timeout: int = DEFAULT_TIMEOUT, model: str = "haiku",
             project_root=None) -> str:
    """Direct LLM API call via llm_client — used in CI where no coding-agent
    CLI exists. Returns the text response or '' on any error (fail-open)."""
    try:
        return llm_client.complete(prompt, tier=model, timeout=timeout,
                                   project_root=project_root)["text"]
    except llm_client.LLMError as e:
        print(f"[archie] api call failed ({e})", file=sys.stderr)
        return ""


_TOOLS = [
    {"name": "read_file", "description": "Read lines from a file in the repo.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "start_line": {"type": "integer"},
         "end_line": {"type": "integer"}}, "required": ["path"]}},
    {"name": "grep", "description": "Regex-search the repo; returns matching path:line.",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "glob": {"type": "string"}},
         "required": ["pattern"]}},
]


def _safe_path(root, rel):
    root = Path(root).resolve()
    try:
        p = (root / rel).resolve()
    except Exception:
        return None
    if root not in p.parents and p != root:
        return None
    if ".git" in p.parts:
        return None
    return p


def _exec_tool(root, name, args):
    if name == "read_file":
        p = _safe_path(root, str(args.get("path", "")))
        if p is None or not p.is_file():
            return "denied: path is outside the repo or not a file"
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "denied: unreadable"
        try:
            s = max(1, int(args.get("start_line", 1)))
            e = min(len(lines), int(args.get("end_line", s + 199)))
        except (TypeError, ValueError):
            return "denied: invalid start_line/end_line"
        return "\n".join(f"{i}: {lines[i-1]}" for i in range(s, e + 1))[:8000]
    if name == "grep":
        import re as _re
        try:
            rx = _re.compile(str(args.get("pattern", "")))
        except Exception:
            return "denied: bad pattern"
        globp = str(args.get("glob", "*.py"))
        root_resolved = Path(root).resolve()
        hits = []
        for f in root_resolved.rglob(globp):
            if not f.is_file():
                continue
            # Jail every candidate the same way read_file is jailed: a symlinked
            # FILE inside the checkout can point outside the repo, and rglob will
            # yield it. relative_to() below only compares lexically, so it would
            # happily relabel the outside file as an in-repo path and leak its
            # contents into the review. Resolve and require real containment.
            rf = f.resolve()
            if (rf != root_resolved and root_resolved not in rf.parents) or ".git" in rf.parts:
                continue
            try:
                for n, ln in enumerate(f.read_text("utf-8", errors="replace").splitlines(), 1):
                    if rx.search(ln):
                        # relative_to must use the same resolved root `f` came
                        # from (rglob), not the caller's raw `root` — on macOS
                        # /tmp is a symlink to /private/tmp, so mixing raw and
                        # resolved paths raises ValueError here.
                        hits.append(f"{f.relative_to(root_resolved)}:{n}: {ln.strip()[:200]}")
                        if len(hits) >= 40:
                            break
            except (OSError, ValueError):
                continue
            if len(hits) >= 40:
                break
        return "\n".join(hits) or "no matches"
    return "denied: unknown tool"


def _run_api_tools(prompt, project_root, model="haiku",
                   timeout=DEFAULT_TIMEOUT, max_turns=6, budget_bytes=60000) -> str:
    """LLM tool-use loop offering jailed read_file/grep against project_root
    (see _exec_tool). Fail-open: returns '' or last seen text, never raises."""
    try:
        return llm_client.complete(
            prompt, tier=model, timeout=timeout, project_root=project_root,
            tools=_TOOLS, max_turns=max_turns, budget_bytes=budget_bytes,
            tool_executor=lambda name, args: _exec_tool(project_root, name, args),
        )["text"]
    except llm_client.LLMError as e:
        print(f"[archie] api tool loop failed ({e})", file=sys.stderr)
        return ""


def detect_cli() -> str:
    """Identify the coding-agent harness driving this run: "claude", "codex",
    or "unknown".

    A pipeline script only ever runs from inside a harness-driven scan, so the
    orchestrating harness IS the signal. Claude Code exports CLAUDECODE=1 to
    every process it spawns; inside a harness-driven run its absence means the
    Codex harness, confirmed by the PATH check. "unknown" is returned only when
    neither signal is present — a unit test or a direct script invocation
    outside any harness — so telemetry records an honest value rather than a
    guess.
    """
    if os.environ.get("CLAUDECODE"):
        return "claude"
    if shutil.which(CODEX_CLI):
        return "codex"
    return "unknown"


def detect_verifier() -> str:
    """Pick the CLI for the headless finding-verifier subagent.

    Shares harness detection with `detect_cli`, but the verifier must always
    have a runnable CLI to shell out to — so an indeterminate environment
    falls back to "claude" (which itself no-ops gracefully if that CLI is
    absent), rather than the "unknown" that `detect_cli` reports for telemetry.
    """
    cli = detect_cli()
    return cli if cli != "unknown" else "claude"


def run_verifier(prompt: str, project_root: Path, verifier: str,
                 timeout: int = DEFAULT_TIMEOUT, model: str = "haiku",
                 tools: bool = False) -> str:
    """Run `prompt` through the selected coding-agent CLI; return its text or "".

    `model` is an alias ("haiku"|"sonnet"|"opus") — the default keeps every existing
    caller on haiku; the invariant specialist requests heavier roles (§6.6a).

    `tools`, when True, only affects the direct-API fallback (path 3 below):
    it swaps in a jailed read_file/grep tool loop (`_run_api_tools`) so a CI
    verifier with no coding-agent CLI can still check claims against the
    checkout. The claude CLI path already has Read/Grep/Glob and ignores this
    flag entirely.

    Priority:
    1. Requested codex CLI (if verifier=='codex' and codex is on PATH).
    2. claude CLI (if available on PATH).
    3. Direct LLM API via `llm_client` (OpenRouter / any OpenAI-compatible
       endpoint / Anthropic — resolved from `.archie/models.json`,
       `ARCHIE_LLM_*`, or `OPENROUTER_API_KEY`/`ANTHROPIC_API_KEY`) — CI
       fallback where no coding-agent CLI is installed.
    4. Empty string — nothing available.
    """
    if verifier == "codex" and shutil.which("codex"):
        return _run_codex(prompt, project_root, timeout)
    if shutil.which("claude"):
        return _run_claude(prompt, project_root, timeout, model=model)
    if llm_client.resolve_config(project_root) is not None:
        if tools:
            return _run_api_tools(prompt, project_root, model=model, timeout=timeout)
        return _run_api(prompt, timeout, model=model, project_root=project_root)
    return ""


def _run_claude(prompt: str, project_root: Path, timeout: int = DEFAULT_TIMEOUT,
                model: str = "haiku") -> str:
    """Spawn `claude -p --model <alias>` synchronously. Returns result text or ""."""
    try:
        proc = subprocess.run(
            [
                CLAUDE_CLI,
                "-p",
                "--model", model,
                "--output-format", "json",
                "--permission-mode", "bypassPermissions",
                "--allowedTools", "Read,Grep,Glob",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=timeout,
            # This spawned `claude -p` fires the project's own UserPromptSubmit
            # hook; the marker tells intent_capture to skip it so Archie's
            # internal prompts never pollute the user-intent log.
            env={**os.environ, "ARCHIE_INTERNAL": "1"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ""
    return envelope.get("result", "") or ""


def _run_codex(prompt: str, project_root: Path, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Spawn `codex exec` synchronously. Returns the model's final text or "".

    `codex exec` runs the prompt non-interactively in a read-only sandbox and
    persists the agent's last message to a file. Send the prompt on stdin
    instead of argv so large cited-file bundles do not hit shell/exec length
    limits.
    """
    tmp_file: str | None = None
    try:
        fd, tmp_file = tempfile.mkstemp(suffix=".codex-out.txt")
        os.close(fd)
        proc = subprocess.run(
            [
                CODEX_CLI,
                "exec",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--output-last-message", tmp_file,
                "-",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=timeout,
            # Mark internal spawn so it can't pollute the user-intent log
            # via the project's own hooks (see _run_claude).
            env={**os.environ, "ARCHIE_INTERNAL": "1"},
        )
        if proc.returncode != 0:
            return ""
        try:
            return Path(tmp_file).read_text().strip()
        except OSError:
            return ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    finally:
        if tmp_file:
            Path(tmp_file).unlink(missing_ok=True)
