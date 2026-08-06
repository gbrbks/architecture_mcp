#!/usr/bin/env node

import { mkdirSync, writeFileSync, readFileSync, existsSync, chmodSync, unlinkSync, readdirSync, rmSync } from "fs";
import { join, resolve, dirname, delimiter } from "path";
import { fileURLToPath } from "url";
import { execSync, spawnSync } from "child_process";
import { stdin, stdout } from "node:process";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const ASSETS = join(__dirname, "..", "assets");

const CYAN = "\x1b[36m";
const GREEN = "\x1b[32m";
const RED = "\x1b[31m";
const DIM = "\x1b[2m";
const RESET = "\x1b[0m";
const BOLD = "\x1b[1m";

const MIN_NODE_MAJOR = 18;
const nodeMajor = parseInt(process.versions.node.split(".")[0], 10);
if (nodeMajor < MIN_NODE_MAJOR) {
  console.error(`Archie requires Node ${MIN_NODE_MAJOR}+. You're on ${process.versions.node}.`);
  process.exit(1);
}

function readPackageVersion() {
  try {
    const pkg = JSON.parse(readFileSync(join(__dirname, "..", "package.json"), "utf8"));
    return pkg.version || "0.0.0";
  } catch { return "0.0.0"; }
}

function streamPrefix(prefix, stream) {
  let buffer = "";
  return (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (line.length) stream.write(`${prefix} ${line}\n`);
    }
  };
}

function runWithPrefix(prefix, cmd, args, opts) {
  const result = spawnSync(cmd, args, { ...opts, stdio: "pipe", encoding: "buffer" });
  const onOut = streamPrefix(prefix, process.stdout);
  const onErr = streamPrefix(prefix, process.stderr);
  if (result.stdout) onOut(result.stdout);
  if (result.stderr) onErr(result.stderr);
  return result.status === 0;
}

function buildLocalViewer(viewerDir, packageVersion) {
  const marker = join(viewerDir, "dist", ".archie-version");
  if (existsSync(marker)) {
    const cached = readFileSync(marker, "utf8").trim();
    if (cached === packageVersion) {
      console.log(`  ${GREEN}✓${RESET} Local viewer up to date (v${packageVersion}) — skipping build`);
      return true;
    }
  }

  console.log("");
  console.log(`${BOLD}  Local viewer setup${RESET} ${DIM}(one-time, ~45s)${RESET}`);
  console.log(`  ${DIM}Installs React deps and builds the UI. Cached by version.${RESET}`);
  console.log("");

  const startedAt = Date.now();

  console.log(`  ${CYAN}→${RESET} Installing dependencies (npm ci)`);
  if (!runWithPrefix(`${DIM}[npm]${RESET}`, "npm", ["ci", "--silent"], { cwd: viewerDir })) {
    console.error("");
    console.error(`  ${BOLD}npm ci failed.${RESET} Common causes:`);
    console.error("    - No internet / corporate proxy blocking npm registry");
    console.error("    - Old node (<18). Run `node --version`.");
    console.error("    - npm misconfigured. Run `npm ping`.");
    return false;
  }

  console.log(`  ${CYAN}→${RESET} Building viewer bundle (vite build)`);
  if (!runWithPrefix(`${DIM}[vite]${RESET}`, "npm", ["run", "build", "--silent"], { cwd: viewerDir })) {
    console.error("");
    console.error(`  ${BOLD}vite build failed.${RESET} See output above.`);
    return false;
  }

  console.log(`  ${CYAN}→${RESET} Cleaning up build dependencies`);
  rmSync(join(viewerDir, "node_modules"), { recursive: true, force: true });

  console.log(`  ${CYAN}→${RESET} Writing version marker`);
  writeFileSync(marker, packageVersion);

  const elapsed = ((Date.now() - startedAt) / 1000).toFixed(0);
  console.log("");
  console.log(`  ${GREEN}✓${RESET} Local viewer built in ${elapsed}s`);
  return true;
}

function cpDirSync(src, dest) {
  mkdirSync(dest, { recursive: true });
  for (const entry of readdirSync(src, { withFileTypes: true })) {
    const s = join(src, entry.name);
    const d = join(dest, entry.name);
    if (entry.isDirectory()) {
      cpDirSync(s, d);
    } else {
      writeFileSync(d, readFileSync(s));
    }
  }
}

const args = process.argv.slice(2);
let projectRootArg = ".";
let commandsDirArg = null;
let targetArg = null;
let detachedMode = false;

const USAGE = `Usage: npx @bitraptors/archie [path] [options]

Installs Archie tooling into the project at <path> (default: current directory).
  path                       Project directory to install into. Defaults to the cwd.
  --target=<spec>            Skip the interactive prompt and install for the given targets.
                             Values: auto | all | claude | codex | comma-separated subset
                             Default (interactive default + non-TTY fallback): all
  --detached                 Skip the prompt and enable detached mode: store
                             generated artifacts in an external folder (~/.archie)
                             and surface them via symlinks, keeping the working
                             tree clean (only .archie-link.json is committed).
                             Experimental. Without this flag an interactive
                             install asks (default: No / repo mode).
  --commands-dir <dir>       Legacy Claude-only override. Multi-CLI installs ignore it.
  -h, --help                 Show this help.`;

for (let i = 0; i < args.length; i++) {
  if (args[i] === "-h" || args[i] === "--help") {
    console.log(USAGE);
    process.exit(0);
  } else if (args[i] === "--commands-dir" && i + 1 < args.length) {
    commandsDirArg = args[i + 1];
    i++;
  } else if (args[i] === "--target" && i + 1 < args.length) {
    targetArg = args[i + 1];
    i++;
  } else if (args[i].startsWith("--target=")) {
    targetArg = args[i].slice("--target=".length);
  } else if (args[i] === "--detached") {
    detachedMode = true;
  } else if (args[i].startsWith("--")) {
    console.error(`Unknown flag: ${args[i]}\n\n${USAGE}`);
    process.exit(2);
  } else {
    projectRootArg = args[i];
  }
}

const HOME = process.env.HOME || process.env.USERPROFILE || "";
const CLI_HOMES = {
  claude: HOME ? join(HOME, ".claude") : "",
  codex: HOME ? join(HOME, ".codex") : "",
};
const CLI_LABELS = { claude: "Claude Code", codex: "Codex CLI" };
const CLI_STATUS = { claude: "stable", codex: "beta" };

function detectCLIs() {
  const detected = [];
  for (const [name, dir] of Object.entries(CLI_HOMES)) {
    if (dir && existsSync(dir)) detected.push(name);
  }
  return detected;
}

// Multi-select TTY picker — Space toggles, arrow keys navigate, Enter confirms.
// Built on raw-mode stdin + ANSI escapes; zero new dependencies.
async function multiSelectPrompt(options, defaultSelectedValues) {
  const selected = new Set(defaultSelectedValues);
  let cursor = 0;

  const renderLine = (opt, i) => {
    const checked = selected.has(opt.value) ? `${GREEN}✓${RESET}` : " ";
    const pointer = i === cursor ? `${CYAN}❯${RESET}` : " ";
    const status = opt.statusDim ? `${DIM}${opt.status}${RESET}` : opt.status;
    return `  ${pointer} [${checked}] ${opt.label.padEnd(16)} ${DIM}${opt.subtitle}${RESET}  ${status}`;
  };

  const draw = () => {
    for (const [i, opt] of options.entries()) {
      stdout.write(renderLine(opt, i) + "\n");
    }
  };

  const redraw = () => {
    // Move cursor up N lines and clear each one as we rewrite it.
    stdout.write(`\x1b[${options.length}A`);
    for (const [i, opt] of options.entries()) {
      stdout.write("\x1b[2K"); // clear entire line
      stdout.write(renderLine(opt, i) + "\n");
    }
  };

  draw();
  stdout.write("\x1b[?25l"); // hide cursor
  stdin.setRawMode(true);
  stdin.resume();
  stdin.setEncoding("utf8");

  return new Promise((resolveResult, rejectResult) => {
    const cleanup = () => {
      stdin.setRawMode(false);
      stdin.pause();
      stdin.removeListener("data", onData);
      stdout.write("\x1b[?25h"); // restore cursor
    };

    const onData = (chunk) => {
      // chunk may carry multiple bytes (e.g. an escape sequence). Iterate.
      const s = chunk.toString("utf8");
      let i = 0;
      while (i < s.length) {
        const c = s[i];
        if (c === "\x03") { // Ctrl+C — bail out cleanly
          cleanup();
          stdout.write("\n");
          process.exit(130);
        } else if (c === "\r" || c === "\n") {
          cleanup();
          resolveResult(Array.from(selected));
          return;
        } else if (c === " ") {
          const opt = options[cursor];
          if (selected.has(opt.value)) selected.delete(opt.value);
          else selected.add(opt.value);
          redraw();
        } else if (c === "a" || c === "A") {
          // toggle all (handy shortcut)
          const all = options.every((o) => selected.has(o.value));
          if (all) selected.clear();
          else options.forEach((o) => selected.add(o.value));
          redraw();
        } else if (c === "\x1b" && s[i + 1] === "[" && (s[i + 2] === "A" || s[i + 2] === "B")) {
          if (s[i + 2] === "A") cursor = (cursor - 1 + options.length) % options.length;
          else cursor = (cursor + 1) % options.length;
          i += 2; // consume "[A" / "[B"
          redraw();
        }
        i++;
      }
    };

    stdin.on("data", onData);
  });
}

async function chooseTargets() {
  // 1. Explicit --target flag wins, always.
  if (targetArg) return targetArg;

  // 2. Non-TTY (CI, piped stdin) → same default as the interactive prompt:
  //    'all'. Keeps behavior consistent across modes — whatever a user
  //    gets by pressing Enter in their terminal is also what CI /
  //    scripted installs produce.
  if (!stdin.isTTY || !stdout.isTTY) return "all";

  // 3. Interactive multi-select. Default = all CLIs selected.
  const detected = detectCLIs();
  const options = ["claude", "codex"].map((cli) => ({
    value: cli,
    label: CLI_LABELS[cli],
    subtitle: detected.includes(cli) ? "detected" : "not detected",
    status: CLI_STATUS[cli],
    statusDim: CLI_STATUS[cli] !== "stable",
  }));

  console.log("");
  console.log(`  ${BOLD}Pick coding-agent CLIs to install Archie for:${RESET}`);
  console.log(`  ${DIM}↑/↓ navigate · space toggles · a toggles all · enter confirms · ctrl-c cancels${RESET}`);
  console.log("");

  const chosen = await multiSelectPrompt(options, ["claude", "codex"]);

  if (chosen.length === 0) {
    console.log(`  ${DIM}⚠ nothing selected — falling back to auto-detect${RESET}`);
    return "auto";
  }
  return chosen.join(",");
}

// Single y/N confirm on raw-mode stdin. Default = No (anything but y/Y).
async function confirmPrompt(label) {
  stdout.write(`  ${BOLD}${label}${RESET} ${DIM}[y/N]${RESET} `);
  stdin.setRawMode(true);
  stdin.resume();
  stdin.setEncoding("utf8");
  return new Promise((res) => {
    const onData = (chunk) => {
      const c = chunk.toString("utf8")[0];
      const finish = (val) => {
        stdin.setRawMode(false);
        stdin.pause();
        stdin.removeListener("data", onData);
        stdout.write(`${val ? "yes" : "no"}\n`);
        res(val);
      };
      if (c === "\x03") { // Ctrl+C
        stdin.setRawMode(false);
        stdin.pause();
        stdout.write("\n");
        process.exit(130);
      } else if (c === "y" || c === "Y") {
        finish(true);
      } else {
        finish(false); // n / Enter / anything else → default No
      }
    };
    stdin.on("data", onData);
  });
}

// Decide whether to enable detached storage mode. Default OFF.
async function chooseDetached() {
  if (detachedMode) return true;                         // explicit --detached
  if (!stdin.isTTY || !stdout.isTTY) return false;       // non-TTY → default off

  console.log("");
  console.log(`  ${BOLD}Artifact storage${RESET}  ${DIM}(experimental)${RESET}`);
  console.log(`  ${DIM}Default: Archie writes its generated files into your repo${RESET}`);
  console.log(`  ${DIM}(CLAUDE.md blocks, .claude/rules/, per-folder CLAUDE.md, .archie/).${RESET}`);
  console.log("");
  console.log(`  ${BOLD}Detached mode${RESET} keeps them OUT of the working tree instead:`);
  console.log(`    ${GREEN}•${RESET} artifacts live in an external store (${CYAN}~/.archie${RESET}) and are`);
  console.log(`      symlinked back in — your tree & diffs stay clean (only`);
  console.log(`      ${CYAN}.archie-link.json${RESET} is committed).`);
  console.log(`    ${GREEN}•${RESET} the viewer gains an ${BOLD}Exposure${RESET} tab to toggle, per file,`);
  console.log(`      what the agent can see (per-folder context + blueprint docs).`);
  console.log(`    ${GREEN}•${RESET} fully reversible — ${DIM}python3 .archie/linker.py detach .${RESET}`);
  console.log(`      copies everything back as real files.`);
  console.log("");
  console.log(`  ${DIM}Not sure? Choose No. You can enable it later by re-running with${RESET}`);
  console.log(`  ${DIM}--detached, or ${RESET}${DIM}python3 .archie/linker.py attach .${RESET} ${DIM}on this install.${RESET}`);
  console.log("");
  return await confirmPrompt("Enable detached mode?");
}

const projectRoot = resolve(projectRootArg);
const archieDir = join(projectRoot, ".archie");
const claudeCommands = join(projectRoot, ".claude", "commands");
const workflowDir = join(archieDir, "workflow");

console.log("");
console.log(`${BOLD}${CYAN}  Archie${RESET} — architecture enforcement for AI coding agents`);
console.log("");

if (commandsDirArg) {
  console.log(`  ${DIM}note: --commands-dir is ignored for multi-CLI installs${RESET}`);
}

mkdirSync(archieDir, { recursive: true });

// Tool-managed .archie/.gitignore — excludes Archie's vendored internals
// (_install_pkg/, viewer/, node_modules, caches) so they're never committed
// into the host repo. Overwritten each install to stay current; it's purely
// tool-internal (users edit their own root .gitignore, not this one).
const gitignoreSrc = join(ASSETS, "gitignore.default");
if (existsSync(gitignoreSrc)) {
  writeFileSync(join(archieDir, ".gitignore"), readFileSync(gitignoreSrc, "utf8"));
  console.log(`  ${GREEN}✓${RESET} .archie/.gitignore (tool internals excluded)`);
}

let cleanedCount = 0;

if (existsSync(archieDir)) {
  for (const f of readdirSync(archieDir)) {
    if (f.endsWith(".py")) {
      try { unlinkSync(join(archieDir, f)); cleanedCount++; } catch {}
    }
  }
}

if (existsSync(claudeCommands)) {
  for (const f of readdirSync(claudeCommands)) {
    if (f.startsWith("archie-") && f.endsWith(".md")) {
      try { unlinkSync(join(claudeCommands, f)); cleanedCount++; } catch {}
    }
  }
}

// Legacy-layout cleanup (.claude/skills/archie-deep-scan/, .archie/prompts/,
// stale command shims/dirs) is owned by the Python install loop —
// see _clean_legacy_layout() in archie/install.py.
if (existsSync(workflowDir)) {
  try { rmSync(workflowDir, { recursive: true, force: true }); cleanedCount++; } catch {}
}

const hooksDir = join(projectRoot, ".claude", "hooks");
if (existsSync(hooksDir)) {
  try { rmSync(hooksDir, { recursive: true, force: true }); cleanedCount++; } catch {}
}

const settingsPath = join(projectRoot, ".claude", "settings.local.json");
if (existsSync(settingsPath)) {
  try {
    const settings = JSON.parse(readFileSync(settingsPath, "utf8"));
    if (settings.hooks) {
      delete settings.hooks;
      writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n");
    }
  } catch {}
}

const platformRulesPath = join(archieDir, "platform_rules.json");
if (existsSync(platformRulesPath)) {
  try { unlinkSync(platformRulesPath); cleanedCount++; } catch {}
}

const installPkgDir = join(archieDir, "_install_pkg");
if (existsSync(installPkgDir)) {
  try { rmSync(installPkgDir, { recursive: true, force: true }); cleanedCount++; } catch {}
}

if (cleanedCount > 0) {
  console.log(`  ${DIM}cleaned ${cleanedCount} previous Archie files${RESET}`);
}

for (const script of ["_common.py", "scanner.py", "refresh.py", "intent_layer.py", "intent.py", "renderer.py", "install_hooks.py", "merge.py", "finalize.py", "validate.py", "viewer.py", "c4.py", "extract_output.py", "arch_review.py", "measure_health.py", "check_rules.py", "detect_cycles.py", "upload.py", "share_setup.py", "telemetry.py", "lint_gate.py", "code_shape.py", "rule_index.py", "align_check.py", "agent_cli.py", "llm_client.py", "verify_findings.py", "apply_verdicts.py", "migrate_blueprint_rules.py", "rule_kinds.py", "backfill_kinds.py", "config.py", "telemetry_sync.py", "update_check.py", "analytics.py", "sync.py", "intent_review.py", "link_store.py", "link_strategy.py", "linker.py", "evidence_schema.py", "diff_basis.py", "selector.py", "editor_gate.py", "reachability.py", "behavioral_review.py", "reconcile.py", "sync_review.py", "delivery_review.py", "intent_capture.py", "invariant_specialist.py", "story_store.py", "story_synthesize.py", "evidence_pack.py", "finding_merge.py", "universal_specialists.py", "review_core.py", "overrides.py", "contract_delta.py"]) {
  const src = join(ASSETS, script);
  const dest = join(archieDir, script);
  if (existsSync(src)) {
    writeFileSync(dest, readFileSync(src, "utf8"));
    chmodSync(dest, 0o755);
    console.log(`  ${GREEN}✓${RESET} .archie/${script}`);
  }
}

for (const dataFile of ["platform_rules.json", "platform_pitfalls.json"]) {
  const src = join(ASSETS, dataFile);
  const dest = join(archieDir, dataFile);
  if (existsSync(src)) {
    writeFileSync(dest, readFileSync(src, "utf8"));
    console.log(`  ${GREEN}✓${RESET} .archie/${dataFile}`);
  }
}

// One-time CI setup helper — runnable as `bash .archie/setup-archie-intent-review.sh`.
for (const helper of ["setup-archie-intent-review.sh"]) {
  const src = join(ASSETS, helper);
  const dest = join(archieDir, helper);
  if (existsSync(src)) {
    writeFileSync(dest, readFileSync(src, "utf8"));
    chmodSync(dest, 0o755);
    console.log(`  ${GREEN}✓${RESET} .archie/${helper}`);
  }
}

// The canonical workflow templates (assets/workflow/) are NOT copied raw —
// the Python install loop renders them per-CLI into .archie/workflow/<cli>/.
const ASSET_SUBDIR_MAP = [
  ["hook_scripts", "hooks"],
  ["_install_pkg", "_install_pkg"],
  ["workflows", "workflows"],   // CI workflow YAMLs (e.g. archie-intent-review.yml)
];
for (const [srcName, destName] of ASSET_SUBDIR_MAP) {
  const src = join(ASSETS, srcName);
  if (existsSync(src)) {
    const dest = join(archieDir, destName);
    if (existsSync(dest)) rmSync(dest, { recursive: true, force: true });
    cpDirSync(src, dest);
    console.log(`  ${GREEN}✓${RESET} .archie/${destName}/ (canonical asset subtree)`);
  }
}

const archieignoreSrc = join(ASSETS, "archieignore.default");
const archieignoreDest = join(projectRoot, ".archieignore");
if (!existsSync(archieignoreDest) && existsSync(archieignoreSrc)) {
  writeFileSync(archieignoreDest, readFileSync(archieignoreSrc, "utf8"));
  console.log(`  ${GREEN}✓${RESET} .archieignore (default patterns)`);
}

const archiebulkSrc = join(ASSETS, "archiebulk.default");
const archiebulkDest = join(projectRoot, ".archiebulk");
if (!existsSync(archiebulkDest) && existsSync(archiebulkSrc)) {
  writeFileSync(archiebulkDest, readFileSync(archiebulkSrc, "utf8"));
  console.log(`  ${GREEN}✓${RESET} .archiebulk (default bulk-content rules)`);
}

const gitignorePath = join(projectRoot, ".gitignore");
// Note: .archie/*.py is intentionally NOT gitignored — scripts are committed so
// the CI delivery-review workflow can read them from the base ref without a local
// Archie install.
const archieGitignoreBlock = `\n# Archie (installed tooling — outputs are NOT ignored)\n.archie/__pycache__/\n.archie/platform_rules.json\n.archie/platform_pitfalls.json\n.archie/workflow/\n.archie/.test_snapshots/\n.claude/commands/archie-*.md\n.claude/hooks/\n.claude/settings.local.json\n.agents/skills/archie-*/\n.codex/hooks.json\n`;

let gitignoreContent = "";
if (existsSync(gitignorePath)) {
  gitignoreContent = readFileSync(gitignorePath, "utf8");
}

if (gitignoreContent.includes("# Archie")) {
  gitignoreContent = gitignoreContent.replace(/\n?# Archie[^\n]*\n(?:[^\n#]*\n)*/m, archieGitignoreBlock);
  writeFileSync(gitignorePath, gitignoreContent);
  console.log(`  ${GREEN}✓${RESET} .gitignore updated (Archie section refreshed)`);
} else {
  writeFileSync(gitignorePath, gitignoreContent + archieGitignoreBlock);
  console.log(`  ${GREEN}✓${RESET} .gitignore updated (Archie tooling ignored)`);
}

let hasPython = false;
try {
  execSync("python3 --version", { stdio: "ignore" });
  hasPython = true;
} catch { /* noop */ }

// The Python connector loop writes the actual deliverable (slash commands +
// rendered workflow). If it is skipped or fails, the install is NOT usable —
// track that and fail loudly at the end instead of printing "Installed!".
let shimsInstalled = false;
let shimFailureReason = "";

if (hasPython) {
  const targetValue = await chooseTargets();
  detachedMode = await chooseDetached();
  const env = {
    ...process.env,
    ARCHIE_ASSETS_ROOT: ASSETS,
    ARCHIE_STANDALONE_ROOT: ASSETS,
    PYTHONPATH: process.env.PYTHONPATH ? `${archieDir}${delimiter}${process.env.PYTHONPATH}` : archieDir,
  };
  console.log("");
  console.log(`  ${DIM}→ python3 -m _install_pkg.install --target=${targetValue}${RESET}`);
  const result = spawnSync("python3", ["-m", "_install_pkg.install", projectRoot, `--target=${targetValue}`], {
    cwd: archieDir,
    env,
    stdio: "inherit",
  });
  if (result.status === 0) {
    shimsInstalled = true;
  } else {
    shimFailureReason = `the Python install step exited with status ${result.status} (see error above). ` +
      "Check `python3 --version` — Archie requires Python 3.9+.";
  }
} else {
  shimFailureReason = "python3 was not found on PATH. Install Python 3.9+ and re-run `npx @bitraptors/archie`.";
}

const viewerSrc = join(ASSETS, "viewer");
const viewerDest = join(archieDir, "viewer");
if (existsSync(viewerSrc)) {
  if (existsSync(viewerDest)) {
    for (const entry of readdirSync(viewerDest, { withFileTypes: true })) {
      if (entry.name === "dist") continue;
      rmSync(join(viewerDest, entry.name), { recursive: true, force: true });
    }
  }
  cpDirSync(viewerSrc, viewerDest);
  console.log(`  ${GREEN}✓${RESET} .archie/viewer/ (React source)`);

  const ok = buildLocalViewer(viewerDest, readPackageVersion());
  if (!ok) {
    console.error("");
    console.error("  ⚠ Local viewer build failed. /archie-viewer will not work.");
    console.error("  ⚠ Other Archie features still work. Re-run `npx @bitraptors/archie`");
    console.error("    after fixing the npm/node issue above.");
  }
}

function writeArchieVersionMarker() {
  const home = process.env.HOME || process.env.USERPROFILE;
  if (!home) return;
  const archieConfigDir = join(home, ".archie");
  const versionFile = join(archieConfigDir, "version");
  const newVersion = readPackageVersion();

  let oldVersion = "";
  try {
    if (existsSync(versionFile)) oldVersion = readFileSync(versionFile, "utf8").trim();
  } catch { /* noop */ }

  try {
    mkdirSync(archieConfigDir, { recursive: true });
    writeFileSync(versionFile, newVersion);
  } catch { return; }

  if (hasPython && oldVersion && oldVersion !== newVersion) {
    const updateScript = join(archieDir, "update_check.py");
    if (existsSync(updateScript)) {
      spawnSync("python3", [updateScript, "mark-upgraded", newVersion, oldVersion], { stdio: "ignore" });
    }
  }
}

writeArchieVersionMarker();

if (!shimsInstalled) {
  console.error("");
  console.error(`${BOLD}\x1b[31m  Install INCOMPLETE — no Claude/Codex commands were written.\x1b[0m`);
  console.error("");
  console.error(`  Reason: ${shimFailureReason}`);
  console.error("");
  console.error("  Support scripts were copied to .archie/, but /archie-deep-scan and the");
  console.error("  other slash commands will NOT appear in your coding agent until this is");
  console.error("  fixed and the installer is re-run.");
  console.error("");
  process.exit(1);
}

// Detached mode: move artifacts to an external store and surface them via
// symlinks so the working tree stays clean. No-op unless --detached was passed.
if (detachedMode) {
  const linkerPath = join(archieDir, "linker.py");
  if (existsSync(linkerPath)) {
    const res = spawnSync("python3", [linkerPath, "bind", projectRoot], {
      stdio: "inherit",
    });
    if (res.status === 0) {
      console.log(`  ${GREEN}✓${RESET} detached mode — artifacts external, tree clean`);
    } else {
      console.error(`  ${RED}✗${RESET} detached bind failed (status ${res.status}); staying in repo mode`);
    }
  } else {
    console.error(`  ${RED}✗${RESET} linker.py not found; cannot enable detached mode`);
  }
}

console.log("");
console.log(`${BOLD}  Installed!${RESET}`);
console.log("");
console.log("  Next steps:");
console.log(`  1. Open this project in ${BOLD}Claude Code${RESET} or ${BOLD}Codex${RESET}`);
console.log(`  2. Run ${BOLD}/archie-deep-scan${RESET} for a comprehensive architecture baseline (15-20 min)`);
console.log(`  3. ${DIM}(optional) Enable PR reviews in CI:${RESET} ${BOLD}bash .archie/setup-archie-intent-review.sh${RESET}`);
console.log(`  ${DIM}Usage: npx @bitraptors/archie [path] [--commands-dir dir]${RESET}`);
console.log("");
console.log(`  ${DIM}This install writes shared Archie assets and delegates CLI shims to the Python connector loop.${RESET}`);
console.log("");
console.log(`  ${DIM}Telemetry: the first Archie command you run asks once when the harness supports prompting.${RESET}`);
console.log(`  ${DIM}whether to share anonymous usage data (opt-in). Nothing is sent until then.${RESET}`);
console.log("");

console.log(`  ${DIM}What gets generated:${RESET}`);
console.log(`  ${DIM}  CLAUDE.md            — architecture context for AI agents${RESET}`);
console.log(`  ${DIM}  AGENTS.md            — multi-agent guidance${RESET}`);
console.log(`  ${DIM}  .claude/hooks/       — real-time architecture enforcement${RESET}`);
console.log(`  ${DIM}  per-folder CLAUDE.md — directory-level context${RESET}`);
console.log("");
