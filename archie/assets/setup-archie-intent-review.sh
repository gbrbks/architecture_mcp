#!/usr/bin/env bash
# setup-archie-intent-review.sh
#
# Idempotent setup for the Archie Intent Review GitHub Action.
# Prereq checks, secure secret setup, workflow install (copies the canonical
# YAML — no embedded duplicate), Actions probe, fork-PR caveat.
#
# Usage: bash setup-archie-intent-review.sh
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-.}"
WORKFLOW_FILE="${REPO_ROOT}/.github/workflows/archie-intent-review.yml"

log_info()    { echo -e "${BLUE}i ${NC}$*"; }
log_success() { echo -e "${GREEN}OK ${NC}$*"; }
log_warn()    { echo -e "${YELLOW}! ${NC}$*"; }
log_error()   { echo -e "${RED}x ${NC}$*"; }
die() { log_error "$1"; exit 1; }

# Resolve the canonical workflow YAML (single source of truth). Priority:
#  1. .archie/workflows/  (if the npx bundle ever places it there)
#  2. <script dir>/workflows/  (running from a checked-out asset bundle)
resolve_workflow_src() {
    local candidates=(
        "${REPO_ROOT}/.archie/workflows/archie-intent-review.yml"
        "${SCRIPT_DIR}/workflows/archie-intent-review.yml"
    )
    for c in "${candidates[@]}"; do
        if [ -f "$c" ]; then printf '%s\n' "$c"; return 0; fi
    done
    return 1
}

# ===== SECTION 1: PREREQUISITES =====
log_info "Checking prerequisites..."

git rev-parse --git-dir >/dev/null 2>&1 || die "Not inside a git repository. Run from the repo root."
log_success "Inside a git repository"

git config --get remote.origin.url >/dev/null 2>&1 || die "No 'origin' remote found."
log_success "Git remote 'origin' found"

command -v gh >/dev/null 2>&1 || die "gh CLI not found. Install from https://github.com/cli/cli or 'brew install gh'."
log_success "gh CLI is installed ($(gh --version | head -1))"

gh auth status >/dev/null 2>&1 || die "gh CLI not authenticated. Run 'gh auth login' first."
GITHUB_ACCOUNT="$(gh api user --jq .login)"
log_success "gh authenticated as ${GITHUB_ACCOUNT}"

[ -f "${REPO_ROOT}/.archie/blueprint.json" ] || die ".archie/blueprint.json not found. Run '/archie-deep-scan' first to establish the baseline."
log_success ".archie/blueprint.json baseline exists"

WORKFLOW_SRC="$(resolve_workflow_src)" || die "Canonical workflow YAML not found (looked in .archie/workflows/ and ${SCRIPT_DIR}/workflows/). Reinstall archie assets."
log_success "Canonical workflow YAML resolved: ${WORKFLOW_SRC}"

# ===== SECTION 2: SECRET SETUP =====
log_info "Choosing the LLM provider for CI reviews..."
echo "  1) OpenRouter  - one key, any model (Claude, GPT, DeepSeek, Gemini, ...) [recommended]"
echo "  2) Anthropic   - direct Anthropic API (Claude models only)"
printf 'Provider [1/2] (default 1): '
read -r PROVIDER_CHOICE
case "${PROVIDER_CHOICE:-1}" in
    2) SECRET_NAME="ANTHROPIC_API_KEY" ;;
    *) SECRET_NAME="OPENROUTER_API_KEY" ;;
esac

log_info "Setting up ${SECRET_NAME} secret (available to GitHub Actions on this repo)..."
printf 'Enter your %s (will not be displayed): ' "$SECRET_NAME"
read -rs API_KEY_VALUE
echo ""
[ -n "$API_KEY_VALUE" ] || die "${SECRET_NAME} cannot be empty."

printf '%s' "$API_KEY_VALUE" | gh secret set "$SECRET_NAME"
unset API_KEY_VALUE
log_success "${SECRET_NAME} secret set (stored encrypted on GitHub)"
log_info "No config file needed: the reviewer auto-detects the provider from which secret exists."

# ===== SECTION 2b: OPTIONAL MODEL PICK =====
if [ "$SECRET_NAME" = "OPENROUTER_API_KEY" ]; then
    DEFAULT_MODEL="anthropic/claude-haiku-4.5"
    MODEL_HINT="any OpenRouter slug, e.g. deepseek/deepseek-v4-flash, google/gemini-2.5-flash, openai/gpt-5-mini"
else
    DEFAULT_MODEL="claude-haiku-4-5"
    MODEL_HINT="an Anthropic model id, e.g. claude-haiku-4-5, claude-sonnet-4-6"
fi
echo ""
log_info "Review model (the workhorse 'haiku' tier; heavier tiers keep their defaults)."
log_info "  ${MODEL_HINT}"
printf 'Model [Enter = %s]: ' "$DEFAULT_MODEL"
read -r REVIEW_MODEL
MODELS_FILE="${REPO_ROOT}/.archie/models.json"
if [ -n "$REVIEW_MODEL" ]; then
    if [ -f "$MODELS_FILE" ]; then
        log_warn "${MODELS_FILE} already exists — not overwriting. Set \"models\": {\"haiku\": \"${REVIEW_MODEL}\"} in it manually."
    else
        PROVIDER_JSON=$([ "$SECRET_NAME" = "ANTHROPIC_API_KEY" ] && echo "anthropic" || echo "openrouter")
        mkdir -p "${REPO_ROOT}/.archie"
        printf '{\n  "provider": "%s",\n  "models": {\n    "haiku": "%s"\n  }\n}\n' "$PROVIDER_JSON" "$REVIEW_MODEL" > "$MODELS_FILE"
        log_success "Wrote ${MODELS_FILE} (commit it so CI uses this model)"
    fi
else
    log_info "Keeping default models (no .archie/models.json needed)."
fi
log_info "More tiers (sonnet/opus) and providers: edit .archie/models.json (see docs/archie-llm-provider-design.md)."

# ===== SECTION 3: WORKFLOW INSTALL (copy canonical, no heredoc) =====
log_info "Installing workflow file..."
mkdir -p "$(dirname "$WORKFLOW_FILE")"
cp "$WORKFLOW_SRC" "$WORKFLOW_FILE"
log_success "Workflow installed at ${WORKFLOW_FILE} (byte-identical to canonical)"

# ===== SECTION 4: ACTIONS ENABLEMENT PROBE (advisory) =====
log_info "Probing GitHub Actions (advisory only)..."
REPO_SLUG="$(git config --get remote.origin.url | sed 's|.*github.com[:/]||; s|\.git$||')"
if gh workflow list -R "$REPO_SLUG" >/dev/null 2>&1; then
    log_success "Actions appear enabled (probe is advisory; verify in repo settings if unsure)"
else
    log_warn "Could not verify Actions status — you may need to enable Actions on GitHub"
fi

# ===== SECTION 5: SUMMARY & CAVEATS =====
log_success "Setup complete."
echo ""
echo "Next steps:"
echo "  1. Commit .github/workflows/archie-intent-review.yml (and .archie/models.json if created)"
echo "  2. Push and open a PR"
echo "  3. The Action posts a single FYI comment on the PR (delivery review):"
echo "       - did the change build the intent (PR title/body) and not break anything?"
echo "       - contract delta: did the blueprint/rules fold weaken a documented invariant?"
echo ""
echo -e "${YELLOW}Fork PR limitation:${NC}"
echo "  - Uses the 'pull_request' event (non-blocking FYI)."
echo "  - Fork PRs cannot access repo secrets; the Action skips silently on them."
echo "  - To cover fork PRs, 'pull_request_target' is a security tradeoff (out of scope)."
echo ""
log_info "To rotate the key later: gh secret set ${SECRET_NAME}"
log_info "To switch provider later: set the other secret (OPENROUTER_API_KEY wins when both exist)."
log_info "Design doc: docs/archie-intent-review-design.md"
