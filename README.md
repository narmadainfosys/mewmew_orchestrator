# mewmew orchestrator

> **mew** — Run **codex**, **opencode**, and **agy** (Google Antigravity CLI) on the same task. In parallel. Or in a pipeline.

A lightweight CLI tool that dispatches work across multiple AI coding agents and collects their results.

```
mew run "refactor auth module and add tests" --agents all
```

## Smart routing stack

`mew smart` is the preferred route for real work. It always uses:

1. **Graphify** to query the project knowledge graph before an agent sees the task.
2. **Claude** to classify task complexity as `simple`, `moderate`, or `complex`.
3. **Headroom** to wrap supported coding CLIs and optimize context.
4. **OmniRoute** as the OpenAI-compatible upstream, using model aliases selected from complexity.
5. **Codex** as the executor.

```bash
# Run in the current checkout so changes land in place
mew smart --no-isolate --yolo "advance this project"

# Show Claude's complexity decision and selected OmniRoute alias only
mew smart --dry-run "fix the failing test"
```

Default complexity-to-model routing:

| Complexity | OmniRoute alias |
|------------|-----------------|
| simple     | `auto/fast`     |
| moderate   | `auto/coding`   |
| complex    | `auto/smart`    |

Override the selected model with `--model`, for example `mew smart --model auto/cheap "..."`.

Start OmniRoute separately when needed:

```bash
omniroute serve
```

Mew points Headroom's OpenAI upstream at `http://127.0.0.1:20128/v1` by default. Override with `MEW_OMNIROUTE_BASE_URL`.

## Install

```bash
pip install mewmew-orchestrator
```

Requires Python 3.10+ and at least one of the supported agents installed:

| Agent        | Install                                                       |
|--------------|---------------------------------------------------------------|
| codex        | `npm install -g @openai/codex`                                |
| opencode     | `brew install anomalyco/tap/opencode` or `npm install -g opencode-ai` |
| agy          | `brew install antigravity-cli` or via Google's installer       |
| graphify     | `pipx install graphifyy` or Graphify's recommended installer   |
| headroom     | `uv tool install --python 3.13 "headroom-ai[all]"`             |
| omniroute    | `npm install -g omniroute`                                    |

## Quick start

```bash
# Run all three agents in parallel
mew run "write unit tests for the payment module" --agents all

# Use specific agents
mew run "deploy this to GKE" --agents agy
mew run "refactor this class" --agents opencode
mew run "quick prototype" --agents codex

# Find the best agent for a task
mew route "design the database schema"

# Show agent profiles
mew profile
```

## Pipeline mode

Chain agents sequentially. By default all steps run in **one shared worktree**,
so a later step sees the files earlier steps actually wrote.

```bash
mew pipeline \
  --assign codex opencode agy \
  --tasks "design the api" "implement it" "write deployment config"
```

With `--no-chain` each step gets its own worktree branched from the same commit.
In that mode a step **cannot** see files another step created — only the previous
agent's prose — so only produce steps that are each independently valid. `mew plan`
tells the planner which mode it is in and warns when a plan looks unrunnable.

## Agents

Built in: `codex`, `opencode`, `claude`, `agy`.

`claude` is set up as the reviewer and merger — auditing branches, reconciling
conflicting implementations, and gating before anything lands.

Each agent declares a `yolo_args` list appended to its command under `--yolo`.
This matters: without an auto-approve flag an agent cannot approve its own tool
calls, so it returns prose and writes no files while appearing to succeed.

## Configuration

Default agents and their commands are built in. Override them with a YAML config:

```bash
mew --config mew.yaml run "..." --agents all
```

Example `mew.yaml`:

```yaml
agents:
  codex:
    description: OpenAI Codex CLI
    strengths: [prototype, debug, python]
    cmd: [codex, exec]
  opencode:
    description: OpenCode (Anthropic)
    strengths: [architecture, review, refactor]
    cmd: [opencode, run]
  agy:
    description: Google Antigravity CLI
    strengths: [gcp, cloud, deploy]
    cmd: [agy, --prompt]
```

Export `MEW_CONFIG=/path/to/mew.yaml` to set it globally.

## How it works

1. **Working directories** — `pipeline` and `plan` share one worktree per run
   (`--chain`, the default) so steps build on each other. `run` gives each agent
   its own worktree, which is the point when you want competing answers.
2. **Dispatch** — agents run in concurrent threads; results are collected and
   displayed together.
3. **Verification** — after every agent, mew reports which files changed and a
   diffstat. A step that changed nothing is distinguishable from one that did
   real work, instead of both looking like a paragraph of prose.
4. **Escape detection** — some agents resolve their own project root and write to
   the real checkout whatever cwd they are given. mew diffs the source checkout
   around each run and reports `WARNING: wrote OUTSIDE its worktree` when that
   happens, rather than leaving you to assume isolation held.
5. **Cleanup** — worktrees and branches are named per run (`mew/<runid>/...`) and
   removed in a `finally`. `mew clean` sweeps up anything left behind.
6. **Crash vs failure** — a negative return code is reported as `crashed` and
   retried once; a non-zero exit is `failed`. They are not the same thing.
7. **Per-agent CLIs differ, and a plausible guess breaks them silently.**
   `codex` needs `exec` plus a bypass flag; `claude` needs `-p` (a boolean, so
   the prompt is positional) plus `--permission-mode bypassPermissions`; and in
   `opencode run`, `-p` means **--password**, so `opencode run -p "<task>"`
   silently swallows the task and exits. Each agent's exact invocation is
   declared in its `cmd`/`yolo_args`; do not guess flags.

## Integration with OpenCode

Install `mew`, then add a custom command to `~/.config/opencode/commands/mew.md`:

```yaml
---
description: Run a multi-agent orchestration task
---

Run: `mew $ARGUMENTS`
```

Now `/mew run "task" --agents all` works inside the OpenCode TUI.

## Output

```text
task: refactor auth module
agents: opencode, codex, agy
mode: parallel

============================================================
  agent #1: opencode
  status:     completed
  returncode: 0
============================================================
[analysis and code changes from opencode...]

============================================================
  agent #2: codex
  status:     completed
  returncode: 0
============================================================
[codex output...]
```

Use `--json` for machine-readable output.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--yolo` | off | Auto-approve all tool permission prompts (per-agent `yolo_args`) |
| `--timeout` | 600s | Per-agent timeout |
| `--dir` | `.mew/` | Working directory for agent outputs |
| `--json` | off | Output results as JSON |
| `--isolate` / `--no-isolate` | on | Worktrees per agent, or run in place |
| `--chain` / `--no-chain` | on | `pipeline`/`plan`: one shared worktree, or one per step |
| `--no-headroom` | — | Call the agent CLI directly instead of routing through the Headroom cost proxy. Use when the proxy's upstream is down or 401-ing, which otherwise makes every agent fail silently |
| `--no-graphify` | — | Skip the graphify knowledge-graph preflight, which is a hard gate and aborts the run when the graph comes out empty |
| `--dry-run` | off | Print the plan, skip execution |

Add `.mew/` to your `.gitignore`.

## Cleaning up

```bash
mew clean          # remove .mew worktrees and mew/* branches
```

## Why "mewmew"?

"mewmew" is my daughter's nickname — this project is named after her. 💛

(It fits, too: orchestrating three AI agents at once is happy little chaos. Mew.)

## License

MIT — see [LICENSE](LICENSE)
