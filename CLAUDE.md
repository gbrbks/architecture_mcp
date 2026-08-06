# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Archie — AI-powered architecture analysis and enforcement for coding agents. Analyzes any codebase, generates a structured blueprint (JSON), and produces: CLAUDE.md, AGENTS.md, per-folder context files, and real-time enforcement hooks. Works with any language, zero dependencies beyond Python 3.9+.

## Repository Layout

- `archie/` — Python package (`archie-cli`): CLI commands, analysis engine, standalone scripts
- `archie/standalone/` — Zero-dependency Python scripts (scanner, renderer, validator, intent layer, health, hooks), llm client (provider-agnostic CI LLM calls: OpenRouter/OpenAI-compatible/Anthropic via `.archie/models.json` + `ARCHIE_LLM_*` env)
- `npm-package/` — NPM distribution (`npx @bitraptors/archie`): copies scripts + Claude Code commands to target projects
- `tests/` — Test suite (pytest)
- `docs/` — Architecture documentation
- `landing/` — Landing page
- `v1/` — Archived V1 web app (FastAPI backend + Next.js frontend, obsolete)

## Storage Modes

Archie supports two artifact-storage modes (opt-in, default `repo`):

- **`repo`** (default) — generated artifacts (CLAUDE.md blocks, `.claude/rules/`,
  `.archie/`, per-folder CLAUDE.md) live in the working tree as today.
- **`detached`** — artifacts live in an external store
  (`$ARCHIE_HOME/projects/<id>/`, default `~/.archie`, `%LOCALAPPDATA%\archie` on
  Windows) and are surfaced into the tree via symlinks (NTFS junctions / copy
  fallback on Windows). The only committed file is `.archie-link.json`
  (`project_id` + `mode`); everything generated is gitignored. The viewer's
  **Exposure** tab (detached mode only) toggles, **per markdown file**, what the
  coding agent can see: `intent_layer` (per-folder `CLAUDE.md`) and `blueprint`
  (`.claude/rules/*.md`). `.archie/` is always-exposed infrastructure (tooling,
  hooks, raw JSON the agent never reads). This is visibility only — it does not
  touch `rules.json` enforcement (a separate viewer feature).

  Manage with `archie/standalone/linker.py`
  (`bind|reconcile|externalize|status|attach|detach`). Enable at install with
  `npx @bitraptors/archie <path> --detached`. Three standalone modules:
  `link_store.py` (store location + manifests), `link_strategy.py` (OS
  presentation primitive + safety invariant), `linker.py` (orchestration + CLI).
  Design: `docs/archie-detached-artifacts-design.md`.

## Commands

### Standalone Scripts
```bash
# Scanner — analyze repository structure
python3 archie/standalone/scanner.py /path/to/project

# Renderer — generate CLAUDE.md, AGENTS.md, rule files from blueprint
python3 archie/standalone/renderer.py /path/to/project

# Validator — check generated output against actual codebase
python3 archie/standalone/validate.py all /path/to/project

# Hooks — install enforcement hooks
python3 archie/standalone/install_hooks.py /path/to/project

# Intent layer — AI-generated per-folder CLAUDE.md via DAG scheduling
python3 archie/standalone/intent_layer.py prepare /path/to/project
python3 archie/standalone/intent_layer.py next-ready /path/to/project
```

### NPM Package
```bash
# Install Archie into a project (copies scripts + commands)
npx @bitraptors/archie /path/to/project

# Then in Claude Code on the target project:
/archie-deep-scan # Comprehensive architecture baseline (15-20 min, run once)
/archie-viewer    # Blueprint inspector
```

### Tests
```bash
python -m pytest tests/ -v
```

### Benchmark Harness (internal)
```bash
# Measure Archie effectiveness: same task, control (no Archie) vs treatment (full Archie)
python3 -m archie.benchmark auto /path/to/repo --prompt "..."   # prep + run from a plain repo
python3 -m archie.benchmark run config.json                     # run on existing branches
```
Internal-only (not shipped via npm). Captures tool calls / tokens / cost / time +
blind judge-Claude quality, writes to Supabase (`benchmark_runs`, `benchmark_samples`).
Before benchmarking, copy `archie/benchmark/secrets.env.example` → `.archie-bench/secrets.env`
and fill in the Supabase URL + service_role key (else runs fall back to offline mode).
See `archie/benchmark/README.md`.

## Command Architecture

- **`/archie-deep-scan`** — Comprehensive baseline (15-20 min). Full 2-wave AI analysis producing blueprint, per-folder CLAUDE.md, rules, and health metrics. Rerun to refresh the baseline; each run builds on prior findings.

## Deep Scan Pipeline (2-Wave)

1. **Scanner** — Counts files, detects frameworks, builds file tree
2. **Wave 1** (parallel) — Sonnet agents gather facts. Structure, Patterns, Technology, **Data**, and **Domain** ALWAYS run; UI Layer is the only optional one (frontend_ratio >= 0.20):
   - Structure agent: Components, layers, file placement
   - Patterns agent: Communication, design patterns, integrations
   - Technology agent: Stack, deployment, dev rules
   - Data agent: Data models, persistence stores, schema-enforced `guarantees`
   - Domain agent: Product behavioral invariants the schema can't enforce → `domain_invariants` (cite-or-omit; balance bounds, lifecycle immutability, idempotency, tenant scoping)
   - UI Layer agent: UI components, state, routing (only if frontend_ratio >= 0.20)
3. **Wave 2** — Reasoning agents (Opus, parallel: Design · Risk · Overview · Product) read all Wave 1 output:
   - Design: decision chain, key decisions with forced_by/enables (linked to the `domain_invariants` each preserves), trade-offs, implementation guidelines
   - Risk: findings + pitfalls (incl. product-law-violation pitfalls)
   - Overview: architecture diagram + executive summary
   - Product (spawns when `domain_invariants` non-empty): `product_model` (domain map) + `derived_invariants` (reasoned laws, ≥2-anchor derivation) + `unenforced_invariants` (ungrounded gap list, advisory only)
4. **Normalize** — AI reshapes raw output to canonical schema
5. **Render** — Deterministic JSON→Markdown (CLAUDE.md, AGENTS.md, rule files)
6. **Validate** — Cross-reference output against actual codebase
7. **Intent Layer** — AI-generated per-folder CLAUDE.md via bottom-up DAG
8. **Finalize** — Health metrics (`health.json` + history), incremental baseline marker, telemetry flush, closing summary

Findings live in `.archie/findings.json` (compounding store: stable ids, verifier + hysteresis in Step 5); `/archie-share` ships them from there. In incremental mode the Risk agent additionally sweeps the changed files against documented invariants and per-folder CLAUDE.md patterns (the recency sweep).

## Key Data Model

Blueprint JSON (`blueprint.json`) contains: `meta`, `architecture_rules`, `decisions` (with `decision_chain`), `components`, `communication`, `quick_reference`, `technology`, `deployment`, `frontend`, `pitfalls`, `implementation_guidelines`, `development_rules`, `architecture_diagram`, `data_models`, `persistence_stores`, and the product-law sections: `domain_invariants` (observed, cited) + `derived_invariants` (reasoned) — both seed `domain_invariant` rules; `product_model` + `unenforced_invariants` are informational only (rendered to `.claude/rules/product-laws.md` + `product-model.md`, never enforced).

## Rules System

Rules are AI-synthesized:

1. **`/archie-deep-scan` Step 6** (Sonnet) — Produces the architectural rule baseline from the blueprint. Each rule carries inline semantic content: `severity_class` (decision_violation / pitfall_triggered / tradeoff_undermined / pattern_divergence / mechanical_violation), `description`, `why` (the reasoning copy-pasted from the motivating blueprint section), `example` (canonical code from `implementation_guidelines.usage_example` when present), and `source: "deep_scan"`. Mechanical rules (regex-checkable housekeeping) also carry `check` + `forbidden_patterns`/`required_in_content` fields.

Additionally, `platform_rules.json` provides universal anti-pattern checks (god functions, force-unwraps, layer rules per platform) installed with every project.

`pre-validate.sh` reads `severity_class` to gate the response: `decision_violation` / `pitfall_triggered` / `mechanical_violation` block (exit 2), `tradeoff_undermined` warns (exit 0 prominent), `pattern_divergence` informs (exit 0 quiet). The agent sees the rule's `description` + `WHY:` block + `EXAMPLE:` block at edit time — no blueprint read required, no pointer indirection. Old-shape rules (no `severity_class`/`why`/`example`) still work via `severity` + `rationale` fallbacks.

The deterministic blueprint extractor (`archie/rules/extractor.py::extract_rules`) was retired in v2.5.0 — its `allowed_dirs` lookup was stale and Step 6 covers placement+naming with full semantic content. Fresh `archie init` writes an empty `rules.json`; users run `/archie-deep-scan` to populate it.

## File Sync

Standalone scripts exist in two places (canonical → copy):
- `archie/standalone/*.py` → `npm-package/assets/*.py`
- `.claude/commands/archie-*.md` (including `archie-deep-scan.md`) → `npm-package/assets/archie-*.md`

Always edit `archie/standalone/` first, then copy to `npm-package/assets/`.
Always edit `.claude/commands/` first, then copy to `npm-package/assets/`.

**Before committing, run the sync checker:**
```bash
python3 scripts/verify_sync.py
```
This verifies all canonical files, asset copies, and `archie.mjs` references are consistent. Catches missing copies, orphan assets, and dead installer references.

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
