# mewmew orchestrator

> **mew** — Run **codex**, **opencode**, and **agy** (Google Antigravity CLI) on the same task. In parallel. Or in a pipeline.

A lightweight CLI tool that dispatches work across multiple AI coding agents and collects their results.

```
mew run "refactor auth module and add tests" --agents all
```

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

Chain agents sequentially. Each step gets the project state plus context from prior steps.

```bash
mew pipeline \
  --assign codex opencode agy \
  --tasks "design the api" "implement it" "write deployment config"
```

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

1. **Isolation** — each agent gets its own git worktree (or directory copy) so they never step on each other's files
2. **Parallel dispatch** — agents run in concurrent threads; results are collected and displayed together
3. **Pipeline** — agents run one after another; each agent's output becomes context for the next

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
| `--yolo` | off | Auto-approve all tool permission prompts |
| `--timeout` | 600s | Per-agent timeout |
| `--dir` | `.mew/` | Working directory for agent outputs |
| `--json` | off | Output results as JSON |
| `--isolate` | on | Create isolated worktrees per agent |
| `--dry-run` | off | Print the plan, skip execution |

## Why "mewmew"?

Because orchestrating three AI agents at once is chaos. Mew.

## License

MIT — see [LICENSE](LICENSE)
