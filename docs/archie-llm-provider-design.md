# LLM Provider Abstraction for CI Review — Design

**Date:** 2026-07-16
**Status:** Approved design, pending implementation plan

## Problem

The CI review pipeline (delivery review + intent review) calls the Anthropic API
directly via raw `urllib` in two places:

- `archie/standalone/agent_cli.py` — `_run_api` (plain Messages call) and
  `_run_api_tools` (tool-use loop with jailed `read_file`/`grep` tools). All
  reviewer fan-out (`review_core.py`, `behavioral_review.py`,
  `invariant_specialist.py`, `universal_specialists.py`) routes through
  `agent_cli.run_verifier`.
- `archie/standalone/intent_review.py` — `call_anthropic` (forced tool choice
  `emit_findings` for structured findings, with 429/5xx retry). Also used by
  `contract_delta.py`.

Model IDs are hardcoded constants (`API_MODELS` in `agent_cli.py`, `MODEL` in
`intent_review.py`). There is no way to use a different provider, swap models,
or toggle them without editing code.

## Goal

Route all CI LLM traffic through one provider-agnostic client with:

- **OpenRouter as the flagship provider**, but any OpenAI-compatible endpoint
  (Vercel AI Gateway, LiteLLM, Groq, Ollama, OpenAI itself) pluggable via
  config.
- **Anthropic direct** retained as a provider — existing installs with only
  `ANTHROPIC_API_KEY` keep working unchanged (no breaking change).
- **Config file + env override** for provider, base URL, and tier→model
  mapping, so switching models is a config edit, not a code edit.
- Zero dependencies (stdlib `urllib` only) — project invariant.

Out of scope: the local CLI fallbacks (`codex exec`, `claude -p`) and the
GitHub comment posting; both are unchanged.

## Design

### New module: `archie/standalone/llm_client.py`

Single entry point used by all callers:

```python
complete(tier, system=None, messages=..., tools=None, tool_choice=None,
         max_tokens=..., tool_executor=None) -> CompletionResult
```

- `tier` is one of the existing aliases `"haiku" | "sonnet" | "opus"`. Callers
  keep speaking in tiers; the tier→model mapping lives in config.
- `tools`/`tool_choice` are expressed in a small neutral shape (name,
  description, JSON schema input; `tool_choice="required:<name>"` for forced
  tool choice). The backend translates to its wire format.
- `tool_executor` (callable `(name, args) -> str`), when given, makes the
  client run the multi-turn tool-use loop internally (replacing
  `_run_api_tools`); the caller supplies the jailed `read_file`/`grep`
  implementations.
- `CompletionResult` carries `text` and `tool_calls` (name + parsed args),
  normalized across backends.

Two protocol backends inside the module:

- **`openai`** — POST `{base_url}/chat/completions`, `Authorization: Bearer`,
  OpenAI `tools`/`tool_calls` format, forced tool choice as
  `{"type": "function", "function": {"name": ...}}`. Default base URL:
  `https://openrouter.ai/api/v1`.
- **`anthropic`** — the existing raw Messages call (`x-api-key`,
  `anthropic-version: 2023-06-01`, content blocks, `stop_reason ==
  "tool_use"`), moved here verbatim in behavior.

Retry (429/5xx/529 honoring `Retry-After`, capped exponential backoff) is
implemented once in the client and applies to both backends — today only
`call_anthropic` has it.

### Configuration

`.archie/models.json` (optional; committed, PR-reviewable):

```json
{
  "provider": "openrouter",
  "base_url": "https://openrouter.ai/api/v1",
  "api_key_env": "OPENROUTER_API_KEY",
  "models": {
    "haiku": "anthropic/claude-haiku-4.5",
    "sonnet": "anthropic/claude-sonnet-4.6",
    "opus": "anthropic/claude-opus-4.8"
  },
  "enabled": true
}
```

- `provider`: `"openrouter"` (alias for openai backend + default base URL),
  `"openai"` (generic OpenAI-compatible; `base_url` required), or
  `"anthropic"`.
- `enabled: false` disables API LLM calls entirely → the pipeline takes its
  existing fail-open path (same as missing key today).
- Env overrides (highest precedence): `ARCHIE_LLM_PROVIDER`,
  `ARCHIE_LLM_BASE_URL`, `ARCHIE_LLM_API_KEY_ENV`, `ARCHIE_MODEL_HAIKU`,
  `ARCHIE_MODEL_SONNET`, `ARCHIE_MODEL_OPUS`.

Resolution order when no config file and no env overrides (backward compat):

1. `OPENROUTER_API_KEY` set → openai backend, OpenRouter base URL, current
   Claude tier models via OpenRouter slugs.
2. `ANTHROPIC_API_KEY` set → anthropic backend, current hardcoded model IDs.
3. Neither → no API path (existing fail-open / CLI-fallback behavior).

### Call-site changes

- `agent_cli.py`: `_run_api` / `_run_api_tools` become thin wrappers over
  `llm_client.complete`; `API_MODELS`/`ANTHROPIC_URL` constants move into the
  client's defaults. `run_verifier` dispatch order unchanged (codex CLI →
  claude CLI → API → empty), except "API available" now means "llm_client has
  a resolvable provider+key".
- `intent_review.py`: `call_anthropic` delegates to `llm_client.complete` with
  `tool_choice="required:emit_findings"`; its public signature stays so
  `contract_delta.py` needs no change (or a mechanical one-line switch).
- Workflows (`.github/workflows/archie-check.yml`,
  `archie/assets/workflows/archie-intent-review.yml` + npm copy): pass
  `OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}` alongside the
  existing `ANTHROPIC_API_KEY`.

### File sync / packaging

- Register `llm_client.py` in `_STANDALONE_SCRIPTS` and `archie.mjs` so it
  ships to `.archie/` in target projects.
- Copy to `npm-package/assets/llm_client.py`; sync all touched files; run
  `python3 scripts/verify_sync.py` before commit.
- Note: deployed projects execute `.archie/*.py` from the PR **base ref**, so
  the new provider path takes effect in a project only after this change is
  merged to its base branch.

## Security

In CI (`GITHUB_ACTIONS` set), `.archie/models.json` is read from the PR-head
checkout — attacker-controlled — while `llm_client.py` itself runs from the
trusted base ref with real secrets (`GITHUB_TOKEN`, provider API keys) in the
environment. If the file's `base_url`/`api_key_env` were honored in CI, a
malicious PR could point the client at an attacker-controlled endpoint and
name `GITHUB_TOKEN` as the key to send it. `resolve_config` therefore ignores
`base_url` and `api_key_env` from the file when `GITHUB_ACTIONS` is set,
keeping only `provider`/`models`/`enabled` from the file; `ARCHIE_LLM_BASE_URL`
/ `ARCHIE_LLM_API_KEY_ENV` env overrides (workflow-controlled) and built-in
provider defaults remain fully honored. Outside CI the file is trusted as-is.

## Error handling

- Missing/empty API key for the resolved provider → return empty result;
  callers' existing fail-open behavior applies (review comment says review was
  skipped, check never fails).
- HTTP 429/500/502/503/529 → retry with backoff (honor `Retry-After`), then
  fail-open.
- Malformed provider response (missing choices/content, unparseable tool
  args) → log to stderr, fail-open empty result.
- Tool loop safety: max-iterations cap (as today) applies on both backends.

## Testing

- Unit tests (`tests/`) for `llm_client`: config resolution precedence
  (file < env), provider auto-detection, request-body construction for both
  backends (assert JSON shape incl. forced tool choice translation), response
  normalization, retry behavior, tool-loop termination — all with a stubbed
  `urllib` opener, no network.
- Regression: existing `intent_review`/`agent_cli` tests keep passing with
  only `ANTHROPIC_API_KEY` set (anthropic backend selected).
- Manual: run `delivery_review.py` against a test PR once with
  `OPENROUTER_API_KEY`, once with `ANTHROPIC_API_KEY`.

## Alternatives considered

- **Per-call-site if/else** — duplicates protocol translation and retry in two
  files; next provider requires touching both. Rejected.
- **OpenRouter's Anthropic-compatible endpoint only** — smallest diff but the
  request stays Anthropic-shaped, so non-Claude models are not reliably
  usable. Rejected: fails the "plug anything in" goal.
