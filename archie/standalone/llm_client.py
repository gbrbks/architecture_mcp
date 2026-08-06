#!/usr/bin/env python3
"""Provider-agnostic LLM client for Archie's CI pipeline.

All standalone-script LLM traffic that hits an HTTP API (the CI path — no
coding-agent CLI on runners) goes through this module. Two wire protocols:

    openai     — POST {base_url}/chat/completions, Bearer auth. This is
                 OpenRouter, Vercel AI Gateway, LiteLLM, Groq, Ollama, OpenAI
                 itself: anything OpenAI-compatible.
    anthropic  — POST api.anthropic.com/v1/messages, x-api-key auth. Kept so
                 existing installs with only ANTHROPIC_API_KEY keep working.

Configuration (first match wins per field):
    1. env overrides: ARCHIE_LLM_PROVIDER / ARCHIE_LLM_BASE_URL /
       ARCHIE_LLM_API_KEY_ENV / ARCHIE_MODEL_{HAIKU,SONNET,OPUS}
    2. {project_root}/.archie/models.json
       {"provider": "openrouter"|"openai"|"anthropic", "base_url": ...,
        "api_key_env": ..., "models": {"haiku"|"sonnet"|"opus": id},
        "enabled": true}
    3. auto-detect: OPENROUTER_API_KEY → openrouter, else
       ANTHROPIC_API_KEY → anthropic, else no API path.

CI trust rule: when GITHUB_ACTIONS is set (detected via the same env= mapping
passed to resolve_config, never os.environ directly), the config file's
"base_url" and "api_key_env" fields are ignored — in CI the file comes from
the PR-head checkout (attacker-controlled) while this code and its secrets
(GITHUB_TOKEN, provider keys) run from the trusted base ref. An attacker who
names api_key_env="GITHUB_TOKEN" and base_url="https://evil" in the file must
not be able to exfiltrate CI secrets to an arbitrary host. "provider",
"models", and "enabled" from the file remain honored in CI (harmless). The
env overrides ARCHIE_LLM_BASE_URL / ARCHIE_LLM_API_KEY_ENV (workflow-controlled,
trusted) and built-in provider defaults are always honored, in CI or not.
Outside CI, the file is fully trusted as before (local Ollama/base_url/
api_key_env setups keep working unchanged).

Callers speak model *tiers* ("haiku"/"sonnet"/"opus"); the tier→model mapping
is config. Zero dependencies (stdlib urllib only) — project invariant.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_URL = "https://openrouter.ai/api/v1"

TIERS = ("haiku", "sonnet", "opus")

ANTHROPIC_MODELS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}
OPENROUTER_MODELS = {
    "haiku": "anthropic/claude-haiku-4.5",
    "sonnet": "anthropic/claude-sonnet-4.6",
    "opus": "anthropic/claude-opus-4.8",
}

DEFAULT_TIMEOUT = 90
DEFAULT_MAX_TOKENS = 4096

_PROVIDER_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "ARCHIE_LLM_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


class LLMError(RuntimeError):
    """Hard LLM failure after retries — callers decide fail-open vs raise."""


def _read_config_file(project_root) -> dict:
    if project_root is None:
        project_root = Path.cwd()
    path = Path(project_root) / ".archie" / "models.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_config(project_root=None, env=None):
    """Resolve provider config; return None when the API path is unavailable
    (no key / disabled) so callers take their existing fail-open branch."""
    if env is None:
        env = os.environ
    file_cfg = _read_config_file(project_root)
    if file_cfg.get("enabled") is False:
        return None

    in_ci = bool(env.get("GITHUB_ACTIONS"))
    if in_ci and (file_cfg.get("api_key_env") or file_cfg.get("base_url")):
        print("[archie] llm: ignoring api_key_env/base_url from .archie/models.json "
              "in CI (untrusted checkout); use ARCHIE_LLM_* env or workflow secrets",
              file=sys.stderr)

    provider = env.get("ARCHIE_LLM_PROVIDER") or file_cfg.get("provider")
    if not provider:
        if env.get("OPENROUTER_API_KEY"):
            provider = "openrouter"
        elif env.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        else:
            return None
    provider = str(provider).strip().lower()
    if provider not in _PROVIDER_KEY_ENV:
        print(f"[archie] llm: unknown provider {provider!r}", file=sys.stderr)
        return None

    key_env = (env.get("ARCHIE_LLM_API_KEY_ENV")
               or (file_cfg.get("api_key_env") if not in_ci else None)
               or _PROVIDER_KEY_ENV[provider])
    api_key = env.get(key_env, "")
    if not api_key:
        return None

    if provider == "anthropic":
        backend, base_url, defaults = "anthropic", ANTHROPIC_URL, ANTHROPIC_MODELS
    else:
        backend, defaults = "openai", OPENROUTER_MODELS
        base_url = (env.get("ARCHIE_LLM_BASE_URL")
                    or (file_cfg.get("base_url") if not in_ci else None)
                    or (OPENROUTER_URL if provider == "openrouter" else ""))
        if not base_url:
            print("[archie] llm: provider 'openai' needs base_url", file=sys.stderr)
            return None
        base_url = base_url.rstrip("/")

    file_models = file_cfg.get("models") if isinstance(file_cfg.get("models"), dict) else {}
    models = {}
    for tier in TIERS:
        models[tier] = (env.get(f"ARCHIE_MODEL_{tier.upper()}")
                        or file_models.get(tier) or defaults[tier])
    return {"backend": backend, "base_url": base_url,
            "api_key": api_key, "models": models}


_RETRYABLE = {429, 500, 502, 503, 529}


def _post_json(url, body, headers, timeout, max_retries):
    """POST JSON with unified retry (429/5xx/529, honoring Retry-After).
    Returns the decoded JSON dict; raises LLMError on hard failure."""
    last_err = "unknown"
    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in _RETRYABLE and attempt < max_retries - 1:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                delay = float(retry_after) if retry_after and str(retry_after).isdigit() \
                    else min(2 ** attempt, 30)
                time.sleep(delay)
                continue
            raise LLMError(f"LLM API error: {last_err}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise LLMError(f"LLM API unreachable: {last_err}")
    raise LLMError(f"LLM API failed: {last_err}")


def complete(prompt=None, *, system=None, tier="haiku", tools=None,
             tool_choice=None, max_tokens=DEFAULT_MAX_TOKENS,
             timeout=DEFAULT_TIMEOUT, config=None, project_root=None,
             tool_executor=None, max_turns=6, budget_bytes=60000,
             max_retries=3):
    """One normalized completion across backends.

    Returns {"text": str, "tool_calls": [{"name", "input"}]}. With
    `tool_executor`, runs the multi-turn tool loop internally (executor gets
    (name, args-dict), returns a string) and returns the final turn's result.
    Raises LLMError on hard failure — callers pick fail-open vs propagate.
    """
    if config is None:
        config = resolve_config(project_root)
    if config is None:
        raise LLMError("no LLM provider configured (no API key / disabled)")
    model = config["models"].get(tier) or config["models"]["haiku"]
    args = dict(model=model, system=system, tools=tools, tool_choice=tool_choice,
                max_tokens=max_tokens, timeout=timeout, config=config,
                tool_executor=tool_executor, max_turns=max_turns,
                budget_bytes=budget_bytes, max_retries=max_retries)
    if config["backend"] == "anthropic":
        return _complete_anthropic(prompt, **args)
    return _complete_openai(prompt, **args)


def _complete_anthropic(prompt, *, model, system, tools, tool_choice, max_tokens,
                        timeout, config, tool_executor, max_turns, budget_bytes,
                        max_retries):
    headers = {"content-type": "application/json", "x-api-key": config["api_key"],
               "anthropic-version": ANTHROPIC_VERSION}
    messages = [{"role": "user", "content": prompt}]
    spent = 0
    last_text = ""
    turns = max_turns if tool_executor else 1
    for turn in range(turns):
        body_d = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            body_d["system"] = system
        if tools:
            body_d["tools"] = tools
        if tool_choice:
            body_d["tool_choice"] = {"type": "tool", "name": tool_choice}
        data = _post_json(config["base_url"], json.dumps(body_d).encode("utf-8"),
                          headers, timeout, max_retries)
        content = data.get("content") or []
        last_text = "".join(b.get("text", "") for b in content
                            if b.get("type") == "text") or last_text
        tool_calls = [{"name": b.get("name"), "input": b.get("input") or {}}
                      for b in content if b.get("type") == "tool_use"]
        if not tool_executor or data.get("stop_reason") != "tool_use":
            return {"text": last_text, "tool_calls": tool_calls}
        # Skip tool execution on the final allowed turn (no further API request will be made)
        if turn == turns - 1:
            return {"text": last_text, "tool_calls": []}
        messages.append({"role": "assistant", "content": content})
        results = []
        for b in content:
            if b.get("type") != "tool_use":
                continue
            if spent >= budget_bytes:
                out = "denied: tool budget exhausted"
            else:
                try:
                    out = tool_executor(b.get("name"), b.get("input") or {})
                except Exception as e:
                    print(f"[archie] llm tool {b.get('name')!r} failed ({type(e).__name__}: {e})", file=sys.stderr)
                    out = "denied: tool call failed"
                spent += len(out)
            results.append({"type": "tool_result", "tool_use_id": b.get("id"),
                            "content": out})
        messages.append({"role": "user", "content": results})
    return {"text": last_text, "tool_calls": []}


def _oai_tools(tools):
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": t.get("input_schema") or {"type": "object"}}} for t in tools]


def _complete_openai(prompt, *, model, system, tools, tool_choice, max_tokens,
                     timeout, config, tool_executor, max_turns, budget_bytes,
                     max_retries):
    url = config["base_url"] + "/chat/completions"
    headers = {"content-type": "application/json",
               "Authorization": f"Bearer {config['api_key']}"}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    spent = 0
    last_text = ""
    turns = max_turns if tool_executor else 1
    for turn in range(turns):
        body_d = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if tools:
            body_d["tools"] = _oai_tools(tools)
        if tool_choice:
            body_d["tool_choice"] = {"type": "function",
                                     "function": {"name": tool_choice}}
        data = _post_json(url, json.dumps(body_d).encode("utf-8"),
                          headers, timeout, max_retries)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"LLM API malformed response: {str(data)[:300]}")
        msg = choices[0].get("message") or {}
        last_text = msg.get("content") or last_text
        raw_calls = msg.get("tool_calls") or []
        tool_calls = []
        for c in raw_calls:
            fn = c.get("function") or {}
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                parsed = raw_args
            else:
                try:
                    parsed = json.loads(raw_args or "{}")
                    if not isinstance(parsed, dict):
                        parsed = {}
                except (ValueError, TypeError):
                    parsed = {}
            tool_calls.append({"name": fn.get("name"), "input": parsed,
                               "_id": c.get("id")})
        public_calls = [{"name": c["name"], "input": c["input"]} for c in tool_calls]
        if not tool_executor or choices[0].get("finish_reason") != "tool_calls":
            return {"text": last_text, "tool_calls": public_calls}
        # Skip tool execution on the final allowed turn (no further API request will be made)
        if turn == turns - 1:
            return {"text": last_text, "tool_calls": []}
        messages.append({"role": "assistant", "content": msg.get("content"),
                         "tool_calls": raw_calls})
        for c in tool_calls:
            if spent >= budget_bytes:
                out = "denied: tool budget exhausted"
            else:
                try:
                    out = tool_executor(c["name"], c["input"])
                except Exception as e:
                    print(f"[archie] llm tool {c['name']!r} failed ({type(e).__name__}: {e})", file=sys.stderr)
                    out = "denied: tool call failed"
                spent += len(out)
            messages.append({"role": "tool", "tool_call_id": c["_id"],
                             "content": out})
    return {"text": last_text, "tool_calls": []}
