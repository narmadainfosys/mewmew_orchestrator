"""
mew - Multi-agent orchestrator for codex, opencode, claude, and agy.

Dispatches coding tasks to multiple AI CLI agents in parallel or pipeline mode.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


DEFAULT_AGENTS = {
    "codex": {
        "description": "OpenAI Codex CLI. Fast prototyping, debugging, Python/TS.",
        "strengths": ["prototype", "debug", "rapid", "quick", "python", "typescript"],
        "cmd": ["codex", "exec"],
        "yolo_args": [
            "--dangerously-bypass-approvals-and-sandbox",
            # Worktrees are valid repos, but a plain copy is not; without this
            # codex refuses to run at all in the non-git fallback path.
            "--skip-git-repo-check",
        ],
        "headroom_target": "codex",
    },
    "opencode": {
        "description": "Anthropic OpenCode. Architecture, review, refactoring, testing, docs.",
        "strengths": ["architecture", "review", "refactor", "test", "document", "plan"],
        "cmd": ["opencode", "run"],
        # No yolo flag: `opencode run` has none, and the plausible guesses are
        # actively harmful — in this subcommand `-p` is --password, so passing
        # "-p <prompt>" swallows the task as a password and opencode exits
        # printing its help, having done nothing. Non-interactive `run` already
        # applies the configured permissions.
        "yolo_args": [],
        "headroom_target": "opencode",
    },
    "claude": {
        "description": (
            "Claude Code CLI. Reviews other agents' work and merges it: audits "
            "branches, reconciles conflicting implementations, and is the gate "
            "before anything lands."
        ),
        "strengths": [
            "review", "merge", "audit", "reconcile", "integration", "verify",
            "elixir", "phoenix", "liveview", "ecto", "architecture",
        ],
        "cmd": ["claude", "-p"],
        # -p/--print is a boolean flag; the prompt arrives as a positional.
        "yolo_args": ["--permission-mode", "bypassPermissions"],
        "headroom_target": "claude",
    },
    "agy": {
        "description": "Google Antigravity CLI. GCP, Android, Kotlin, Go, cloud infra.",
        "strengths": ["gcp", "android", "kotlin", "go", "cloud", "kubernetes", "deploy"],
        "cmd": ["agy", "--prompt"],
        "yolo_args": ["--dangerously-skip-permissions"],
        "headroom_target": None,
    },
}

# Agents configured before `yolo_args` existed keep working: these are the
# flags that were hardcoded per agent name.
LEGACY_YOLO_ARGS = {
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "agy": ["--dangerously-skip-permissions"],
}

OMNIROUTE_BASE_URL = os.environ.get("MEW_OMNIROUTE_BASE_URL", "http://127.0.0.1:20128/v1")
HEADROOM_BASE_PORT = int(os.environ.get("MEW_HEADROOM_PORT", "8787"))
HEADROOM_PORTS = {"codex": HEADROOM_BASE_PORT, "claude": HEADROOM_BASE_PORT + 1, "opencode": HEADROOM_BASE_PORT + 2}
COMPLEXITY_MODELS = {
    "simple": "auto/fast",
    "moderate": "auto/coding",
    "complex": "auto/smart",
}


def load_config(path: str | None = None) -> dict:
    config = DEFAULT_AGENTS.copy()
    if path:
        if yaml is None:
            # Silently ignoring an explicit --config means dispatching to agents
            # the caller never asked for. Fail instead.
            raise SystemExit(
                f"error: --config {path} needs PyYAML, which is not installed.\n"
                "       pip install 'mewmew-orchestrator[yaml]'"
            )
        try:
            with open(path) as f:
                overrides = yaml.safe_load(f) or {}
        except FileNotFoundError:
            raise SystemExit(f"error: config {path} not found")
        config.update(overrides.get("agents", {}))
    return config


def find_best_agent(task: str, agents: list[str], config: dict) -> list[str]:
    task_lower = task.lower()
    scored: list[tuple[int, str]] = []
    for name in agents:
        strengths = config.get(name, {}).get("strengths", [])
        score = sum(2 for s in strengths if s in task_lower)
        scored.append((score or 1, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [name for _, name in scored]


def extract_json(text: str) -> list[dict] | None:
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    for pattern in [r'```(?:json)?\s*\n(.*?)```', r'(\[.*?\])', r'(\{.*\})']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    for pattern in [r'```(?:json)?\s*\n(.*?)```', r'(\{.*\})']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def graphify_context(prompt: str, workdir: str, budget: int = 1800) -> tuple[bool, str]:
    """Return scoped Graphify context. This is mandatory for mew agent runs."""
    if not shutil.which("graphify"):
        return False, "graphify is required but not found on PATH"

    graph = Path(workdir) / "graphify-out" / "graph.json"
    if not graph.exists():
        extract = subprocess.run(
            ["graphify", "extract", ".", "--code-only", "--out", "."],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if extract.returncode != 0:
            err = (extract.stderr or extract.stdout or "graphify extract failed").strip()
            return False, err

    query = prompt[:1200]
    r = subprocess.run(
        ["graphify", "query", query, "--graph", str(graph), "--budget", str(budget)],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "graphify query failed").strip()
        return False, err
    return True, (r.stdout or "").strip()[:8000]


def with_graphify(prompt: str, workdir: str) -> tuple[bool, str, str]:
    ok, ctx = graphify_context(prompt, workdir)
    if not ok:
        return False, prompt, ctx
    wrapped = (
        f"{prompt}\n\n"
        "<graphify_context mandatory=\"true\">\n"
        f"{ctx}\n"
        "</graphify_context>\n"
    )
    return True, wrapped, ""


def build_agent_cmd(name: str, agent: dict, prompt: str, workdir: str, yolo: bool,
                    headroom: bool = True) -> tuple[bool, list[str], str]:
    base_cmd = list(agent["cmd"])
    if yolo:
        yolo_args = agent.get("yolo_args")
        if yolo_args is None:
            yolo_args = LEGACY_YOLO_ARGS.get(name, [])
        base_cmd.extend(yolo_args)

    if name == "agy":
        base_cmd.extend(["--add-dir", workdir])

    cmd = base_cmd + [prompt]
    if not headroom:
        # Headroom is a cost-saving proxy in front of the agent. It is a policy
        # choice, not a correctness requirement: when its upstream is down or
        # unauthenticated every agent fails with a 401 and no files are written,
        # so the bypass has to exist and it has to be visible in the output.
        return True, cmd, ""
    target = agent.get("headroom_target")
    if target is None:
        return False, cmd, f"{name} is not supported by Headroom wrap; mandatory Headroom would be bypassed"
    port = HEADROOM_PORTS.get(target, HEADROOM_BASE_PORT)
    wrapped = ["headroom", "wrap", target, "--port", str(port), "--"] + cmd[1:]
    return True, wrapped, ""


def agent_env(headroom: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    if headroom:
        env.setdefault("OPENAI_TARGET_API_URL", OMNIROUTE_BASE_URL)
        env.setdefault("OPENAI_API_KEY", env.get("OMNIROUTE_API_KEY", "omniroute-local"))
        env.setdefault("HEADROOM_SAVINGS_PROFILE", "coding")
    else:
        # Withdraw only the routing mew itself would have injected. The real
        # OPENAI_API_KEY is the agent's own credential and must survive, or the
        # bypass turns a proxy error into an authentication error.
        for k in ("OPENAI_TARGET_API_URL", "HEADROOM_SAVINGS_PROFILE"):
            env.pop(k, None)
    return env


def run_id() -> str:
    """Identifies one invocation, so branches never collide across runs."""
    return time.strftime("%Y%m%d-%H%M%S")


def git_status(cwd: str, ignore_prefix: str = "") -> str:
    r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=cwd)
    out = r.stdout if r.returncode == 0 else ""
    if ignore_prefix:
        # The worktrees live inside base_dir, which normally sits inside the
        # repo, so they show up as untracked in the source checkout. Counting
        # those as "the agent escaped" would flag every single run.
        out = "\n".join(
            ln for ln in out.splitlines()
            if not _porcelain_path(ln).startswith(ignore_prefix)
        )
    return out


def _porcelain_path(line: str) -> str:
    """Pull the path out of a `git status --porcelain` line.

    Porcelain v1 is exactly "XY <path>": two status columns and one space.
    Stripping the line first shifts that prefix by one and eats the first
    character of the filename (" M app.js" -> "pp.js").
    """
    path = line[3:].strip()
    if " -> " in path:  # rename/copy: keep the destination
        path = path.split(" -> ", 1)[1]
    return path.strip('"')


def changed_paths(cwd: str) -> list[str]:
    return [_porcelain_path(ln) for ln in git_status(cwd).splitlines() if ln.strip()]


def diff_summary(cwd: str) -> str:
    r = subprocess.run(["git", "diff", "--stat", "HEAD"], capture_output=True, text=True, cwd=cwd)
    return (r.stdout or "").strip()


def add_worktree(src: str, path: str, branch: str) -> tuple[bool, str]:
    """Create a git worktree. Reports failure instead of silently copying the
    tree, which used to drag _build/ and deps/ along and hand the agent a
    directory that was not the worktree it thought it was in."""
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["git", "worktree", "add", "-b", branch, path],
        capture_output=True, text=True, cwd=src,
    )
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout or "worktree add failed").strip()


def remove_worktree(path: str) -> None:
    r = subprocess.run(["git", "worktree", "remove", "--force", path], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)


def run_agent(name: str, prompt: str, config: dict, workdir: str, timeout: int, yolo: bool,
              headroom: bool = True, graphify: bool = True) -> dict[str, Any]:
    agent = config.get(name)
    if not agent:
        return {"agent": name, "status": "error", "error": f"unknown agent: {name}", "workdir": workdir}

    result: dict[str, Any] = {
        "agent": name, "status": "running", "output": "", "error": "",
        "workdir": workdir, "returncode": -1, "files": [], "escaped": False,
    }
    ok, prompt, graphify_error = with_graphify(prompt, workdir) if graphify else (True, prompt, "")
    if not ok:
        result["status"] = "error"
        result["error"] = f"Graphify preflight failed: {graphify_error}"
        return result

    ok, cmd, command_error = build_agent_cmd(name, agent, prompt, workdir, yolo, headroom)
    if not ok:
        result["status"] = "error"
        result["error"] = command_error
        return result

    env = agent_env(headroom)

    try:
        proc = subprocess.Popen(
            cmd, cwd=workdir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def reader(stream, lines, tag):
            try:
                for line in iter(stream.readline, ""):
                    lines.append(line)
                    print(f"[{tag}] {line}", end="", flush=True)
            finally:
                stream.close()

        t_out = threading.Thread(target=reader, args=(proc.stdout, stdout_lines, name))
        t_err = threading.Thread(target=reader, args=(proc.stderr, stderr_lines, f"{name}:err"))
        t_out.start()
        t_err.start()

        proc.wait(timeout=timeout)
        t_out.join()
        t_err.join()

        # A negative returncode means the process died on a signal. That is a
        # crash, not a considered failure, and is worth retrying and saying so.
        if proc.returncode == 0:
            result["status"] = "completed"
        elif proc.returncode is not None and proc.returncode < 0:
            result["status"] = "crashed"
            result["error"] = f"killed by signal {abs(proc.returncode)}"
        else:
            result["status"] = "failed"
        result["output"] = "".join(stdout_lines)
        result["error"] = "".join(stderr_lines)
        result["returncode"] = proc.returncode

    except subprocess.TimeoutExpired:
        proc.kill()
        t_out.join()
        t_err.join()
        result["status"] = "timeout"
        result["error"] = f"timed out after {timeout}s"
        result["output"] = "".join(stdout_lines)
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def classify_task(task: str, config: dict, workdir: str, timeout: int, yolo: bool, allow_fallback: bool = False) -> dict[str, Any]:
    prompt = f"""\
Classify the complexity of this software task for model routing.

Return ONLY a JSON object with:
- complexity: one of "simple", "moderate", "complex"
- reason: one short sentence

Use "simple" for quick edits, typo fixes, small docs, or low-risk local changes.
Use "moderate" for normal implementation/debugging across a few files.
Use "complex" for architecture, broad refactors, migrations, ambiguous debugging, or high-risk changes.

Task: {task}
"""
    result = run_agent("claude", prompt, config, workdir, timeout, yolo, headroom, graphify)
    decision = extract_json_object(result.get("output", ""))
    if result.get("status") != "completed" or not decision:
        if not allow_fallback:
            return {
                "error": "Claude complexity classification failed; refusing to route without Claude.",
                "classifier": result,
            }
        return {
            "complexity": "moderate",
            "reason": "Claude classification failed; using conservative default.",
            "classifier": result,
        }
    complexity = str(decision.get("complexity", "moderate")).lower()
    if complexity not in COMPLEXITY_MODELS:
        complexity = "moderate"
    return {
        "complexity": complexity,
        "reason": str(decision.get("reason", "")).strip() or "No reason returned.",
        "classifier": result,
    }


def prepare_workdirs(base_dir: str, agents: list[str], rid: str, src: str) -> tuple[dict[str, str], list[dict]]:
    """One worktree per agent, on branches unique to this run."""
    dirs: dict[str, str] = {}
    failures: list[dict] = []
    shutil.rmtree(base_dir, ignore_errors=True)
    Path(base_dir).mkdir(parents=True, exist_ok=True)

    in_git = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True).returncode == 0

    for name in agents:
        if in_git and shutil.which("git"):
            wt_path = os.path.join(base_dir, f"wt-{name}")
            ok, err = add_worktree(src, wt_path, f"mew/{rid}/{name}")
            if ok:
                dirs[name] = wt_path
            else:
                failures.append({"agent": name, "status": "error", "error": f"worktree failed: {err}"})
            continue
        agent_dir = os.path.join(base_dir, f"agent-{name}")
        if os.path.exists(agent_dir):
            shutil.rmtree(agent_dir)
        shutil.copytree(src, agent_dir, ignore=shutil.ignore_patterns(".git", "node_modules", ".mew"))
        dirs[name] = agent_dir

    return dirs, failures


def cleanup_workdirs(base_dir: str, agents: list[str], rid: str) -> None:
    for name in agents:
        remove_worktree(os.path.join(base_dir, f"wt-{name}"))
        subprocess.run(["git", "branch", "-D", f"mew/{rid}/{name}"], capture_output=True, cwd=os.getcwd())


def _ignore_prefix(src: str, base_dir: str) -> str:
    try:
        rel = os.path.relpath(os.path.abspath(base_dir), os.path.abspath(src))
    except ValueError:
        return ""
    return "" if rel.startswith("..") else rel


def _audit(result: dict, src: str, src_before: str, isolated: bool, ignore: str = "") -> dict:
    """Record what actually changed, and whether the agent escaped its worktree.

    Agents with their own project-root detection (opencode, codex) write to the
    real checkout no matter what cwd they are given, so the isolation mew
    advertises is not something the agent is actually obeying. Detecting it is
    the only way the result can be trusted.
    """
    workdir = result.get("workdir", "")
    if os.path.isdir(workdir) and git_status(workdir):
        result["files"] = changed_paths(workdir)
        result["diffstat"] = diff_summary(workdir)

    if isolated and os.path.abspath(workdir) != os.path.abspath(src):
        after = git_status(src, ignore)
        if after != src_before:
            new = [l for l in after.splitlines() if l not in src_before.splitlines()]
            result["escaped"] = True
            result["escaped_files"] = [_porcelain_path(l) for l in new if l.strip()]
    return result


def parallel_dispatch(prompt: str, agents: list[str], config: dict, base_dir: str, timeout: int, yolo: bool, isolate: bool, headroom: bool = True, graphify: bool = True) -> list[dict]:
    results: list[dict] = []
    threads: list[threading.Thread] = []
    lock = threading.Lock()
    src = os.getcwd()
    rid = run_id()
    ignore = _ignore_prefix(src, base_dir)
    src_before = git_status(src, ignore)

    if isolate:
        dirs, failures = prepare_workdirs(base_dir, agents, rid, src)
        results.extend(failures)
    else:
        # --no-isolate means run where the user is. base_dir is for mew's own
        # bookkeeping, not the place the agent should be editing.
        Path(base_dir).mkdir(parents=True, exist_ok=True)
        dirs = {name: src for name in agents}

    def worker(name: str):
        r = run_agent(name, prompt, config, dirs[name], timeout, yolo, headroom, graphify)
        if r["status"] == "crashed":
            print(f"[mew] {name} crashed, retrying once", flush=True)
            r = run_agent(name, prompt, config, dirs[name], timeout, yolo, headroom, graphify)
        with lock:
            results.append(_audit(r, src, src_before, isolate, ignore))

    try:
        for name in agents:
            if name not in dirs:
                continue
            t = threading.Thread(target=worker, args=(name,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
    finally:
        if isolate:
            cleanup_workdirs(base_dir, agents, rid)

    return results


def pipeline_dispatch(steps: list[tuple[str, str]], config: dict, base_dir: str, timeout: int, yolo: bool, chain: bool = True, isolate: bool = True, headroom: bool = True, graphify: bool = True) -> list[dict]:
    """Run steps in order.

    With chain=True every step runs in the same worktree, so a later step sees
    the files the earlier ones actually wrote. That is what "each step gets the
    project state plus context from prior steps" is supposed to mean, and it is
    the only mode in which dependent steps can work at all: with separate
    worktrees every step branches from the same commit and can only see the
    previous agent's prose.
    """
    results: list[dict] = []
    shutil.rmtree(base_dir, ignore_errors=True)
    Path(base_dir).mkdir(parents=True, exist_ok=True)
    context = ""
    src = os.getcwd()
    rid = run_id()
    ignore = _ignore_prefix(src, base_dir)
    src_before = git_status(src, ignore)

    in_git = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True).returncode == 0

    shared_dir: str | None = None
    if not isolate:
        # Run where the user is, like `run --no-isolate`.
        shared_dir = src
    elif chain:
        if in_git and shutil.which("git"):
            ok, err = add_worktree(src, os.path.join(base_dir, "chain"), f"mew/{rid}/chain")
            if not ok:
                print(f"[mew] chain worktree failed: {err}", file=sys.stderr, flush=True)
                return [{"agent": "chain", "status": "error", "error": err, "workdir": base_dir}]
            shared_dir = os.path.join(base_dir, "chain")
        else:
            shared_dir = base_dir

    try:
        for i, (agent_name, task) in enumerate(steps):
            if shared_dir:
                step_dir = shared_dir
            else:
                step_dir = os.path.join(base_dir, f"step-{i+1}-{agent_name}")
                if in_git and shutil.which("git"):
                    ok, err = add_worktree(src, step_dir, f"mew/{rid}/step{i+1}-{agent_name}")
                    if not ok:
                        results.append({
                            "agent": agent_name, "status": "error",
                            "error": f"worktree failed, agent not run: {err}",
                            "workdir": step_dir, "returncode": -1, "files": [], "escaped": False,
                        })
                        continue
                elif os.path.exists(step_dir):
                    shutil.rmtree(step_dir)
                else:
                    shutil.copytree(src, step_dir, ignore=shutil.ignore_patterns(".git", "node_modules", ".mew"))

            if not os.path.isdir(step_dir):
                results.append({
                    "agent": agent_name, "status": "error",
                    "error": f"workdir does not exist, agent not run: {step_dir}",
                    "workdir": step_dir, "returncode": -1, "files": [], "escaped": False,
                })
                continue

            prompt = f"{task}\n\n<context>\n{context[:3000]}\n</context>" if context else task
            r = run_agent(agent_name, prompt, config, step_dir, timeout, yolo, headroom, graphify)
            if r["status"] == "crashed":
                print(f"[mew] step {i+1} ({agent_name}) crashed, retrying once", flush=True)
                r = run_agent(agent_name, prompt, config, step_dir, timeout, yolo, headroom, graphify)
            results.append(_audit(r, src, src_before, isolated=bool(shared_dir), ignore=ignore))
            context += f"\n--- Step {i+1} ({agent_name}) ---\n{r['output'][:3000]}"
    finally:
        if shared_dir is None:
            # Unchained: one worktree per step, each needing its own teardown.
            for i, (agent_name, _) in enumerate(steps):
                remove_worktree(os.path.join(base_dir, f"step-{i+1}-{agent_name}"))
                subprocess.run(
                    ["git", "branch", "-D", f"mew/{rid}/step{i+1}-{agent_name}"],
                    capture_output=True, cwd=src,
                )
        elif shared_dir not in (src, base_dir):
            remove_worktree(shared_dir)
            subprocess.run(["git", "branch", "-D", f"mew/{rid}/chain"], capture_output=True, cwd=src)

    return results


PLANNER_PROMPT = """\
You are a planner that breaks development tasks into clear sub-tasks and assigns each to the best agent.

Available agents:
{profiles}

{chaining}

Rules:
- Each sub-task targets ONE agent — pick the agent whose strengths fit best
- Keep sub-tasks focused and actionable
- Each sub-task must name the files it owns, so overlapping ownership is visible

Respond with ONLY a valid JSON array. No markdown, no explanation, no code blocks:
[
  {{"agent": "<agent_name>", "task": "<concise sub-task description>"}}
]

Task: {task}"""

CHAINED_RULES = """\
How the steps will run: SEQUENTIALLY IN ONE SHARED WORKTREE.
A later step sees every file the earlier steps wrote. So it is correct, and
expected, to order steps by dependency: create a module before the code that
calls it, write the implementation before the tests that exercise it, and put
any final verification or cleanup step last."""

UNCHAINED_RULES = """\
How the steps will run: EACH IN ITS OWN GIT WORKTREE, BRANCHED FROM THE SAME
COMMIT. A step CANNOT see any file another step created — only the previous
agent's prose summary, truncated.

Therefore:
- Do NOT create a step that depends on a file an earlier step creates. A step
  whose tests need a module another step writes will not compile.
- Do NOT order steps by dependency "dependencies first"; that is exactly wrong
  here and produces plans that cannot run.
- Only produce steps that are each independently valid on their own: work on
  files that already exist, or that the same step also creates."""


def plan_dispatch(plan: list[dict], config: dict, base_dir: str, timeout: int, yolo: bool, chain: bool = True, isolate: bool = True, headroom: bool = True, graphify: bool = True) -> list[dict]:
    steps = [(s["agent"], s["task"]) for s in plan]
    return pipeline_dispatch(steps, config, base_dir, timeout, yolo, chain=chain, isolate=isolate)


def format_results(results: list[dict], mode: str = "run") -> str:
    lines = [f"[mew] mode: {mode.upper()}", f"[mew] agents: {len(results)}"]

    for i, r in enumerate(results):
        lines.extend([
            "",
            "=" * 60,
            f"  agent #{i+1}: {r['agent']}",
            f"  status:     {r['status']}",
            f"  returncode: {r.get('returncode')}",
        ])
        if r.get("escaped"):
            files = ", ".join(r.get("escaped_files", [])[:10]) or "unknown"
            lines.append("  WARNING:     wrote OUTSIDE its worktree — worktree isolation was not honoured")
            lines.append(f"  escaped to:  {files}")
        files = r.get("files") or []
        if files:
            shown = ", ".join(files[:12])
            more = f" (+{len(files) - 12} more)" if len(files) > 12 else ""
            lines.append(f"  files ({len(files)}): {shown}{more}")
        else:
            lines.append("  files:       none changed")
        if r.get("diffstat"):
            lines.append("  diffstat:")
            lines.extend(f"    {ln}" for ln in r["diffstat"].splitlines())
        lines.append("=" * 60)

        output = (r.get("output") or "").strip()
        error = (r.get("error") or "").strip()
        if output:
            lines.append(output)
        if error:
            lines.append(f"[stderr]\n{error}")

    lines.append("\n" + "=" * 60)
    landed = sum(1 for r in results if r.get("files"))
    escaped = sum(1 for r in results if r.get("escaped"))
    lines.append(f"[mew] done — {landed}/{len(results)} changed files, {escaped} escaped isolation")
    return "\n".join(lines)


def format_json(results: list[dict]) -> str:
    return json.dumps(results, indent=2)


COMMANDS = {}


def command(fn):
    COMMANDS[fn.__name__.replace("cmd_", "")] = fn
    return fn


FILE_RE = re.compile(r"[\w./-]+\.(?:ex|exs|js|ts|css|heex|json|ya?ml|md)")


def plan_warnings(plan: list[dict], chain: bool) -> list[str]:
    """Catch the two ways a generated plan cannot actually run."""
    warnings: list[str] = []
    if not chain:
        for i, step in enumerate(plan):
            for j in range(i + 1, len(plan)):
                a = set(FILE_RE.findall(step.get("task", "")))
                b = set(FILE_RE.findall(plan[j].get("task", "")))
                shared = a & b
                if shared:
                    warnings.append(
                        f"  steps {i+1} and {j+1} both touch {', '.join(sorted(shared))}"
                        " — in unchained mode they cannot see each other"
                    )
    owners: dict[str, list[int]] = {}
    for i, step in enumerate(plan):
        for f in set(FILE_RE.findall(step.get("task", ""))):
            owners.setdefault(f, []).append(i)
    for f, idx in owners.items():
        if len(idx) > 1 and chain:
            warnings.append(
                f"  {f} is written by {len(idx)} steps ({', '.join(str(n+1) for n in idx)})"
                " — later writes will overwrite earlier ones in the shared worktree"
            )
    return warnings


@command
def cmd_clean(args: argparse.Namespace, config: dict) -> int:
    base_dir = args.dir or os.path.join(os.getcwd(), ".mew")
    removed = False
    if os.path.isdir(base_dir):
        for entry in sorted(os.listdir(base_dir)):
            remove_worktree(os.path.join(base_dir, entry))
        shutil.rmtree(base_dir, ignore_errors=True)
        removed = True
    r = subprocess.run(["git", "worktree", "prune"], capture_output=True, text=True)
    # `git branch --list mew` will not match "mew-opencode" (git matches ref
    # path components, not string prefixes), which is exactly the legacy
    # naming that needs sweeping, so glob the ref namespace directly.
    branches = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/mew*"],
        capture_output=True, text=True,
    ).stdout.split()
    for b in branches:
        subprocess.run(["git", "branch", "-D", b], capture_output=True)
    print(f"[mew] cleaned {base_dir} ({len(branches)} mew branches, worktree prune rc={r.returncode})"
          if removed else f"[mew] nothing at {base_dir}; pruned {len(branches)} mew branches")
    return 0


@command
def cmd_run(args: argparse.Namespace, config: dict) -> int:
    agents = args.agents
    if "all" in agents:
        agents = list(config.keys())

    base_dir = args.dir or os.path.join(os.getcwd(), ".mew")

    print(f"task: {args.prompt}", flush=True)
    print(f"agents: {', '.join(agents)}", flush=True)
    print(f"mode: parallel", flush=True)
    print(f"dir: {base_dir}", flush=True)
    print(f"isolate: {args.isolate}", flush=True)

    if args.dry_run:
        print("[dry-run] skipping execution", flush=True)
        return 0

    results = parallel_dispatch(args.prompt, agents, config, base_dir, args.timeout, args.yolo, args.isolate, args.headroom, args.graphify)
    output = format_json(results) if args.json else format_results(results)
    print(output, flush=True)
    return 0


@command
def cmd_pipeline(args: argparse.Namespace, config: dict) -> int:
    steps = list(zip(args.assign, args.tasks))
    base_dir = args.dir or os.path.join(os.getcwd(), ".mew")

    print(f"pipeline: {len(steps)} steps", flush=True)
    for i, (agent, task) in enumerate(steps):
        print(f"  step {i+1}: {agent} <- {task}", flush=True)
    print(f"chain: {args.chain}", flush=True)

    if args.dry_run:
        print("[dry-run] skipping execution", flush=True)
        return 0

    results = pipeline_dispatch(steps, config, base_dir, args.timeout, args.yolo, chain=args.chain, isolate=args.isolate, headroom=args.headroom, graphify=args.graphify)
    output = format_json(results) if args.json else format_results(results, mode="pipeline")
    print(output, flush=True)
    return 0


@command
def cmd_route(args: argparse.Namespace, config: dict) -> int:
    agents = args.agents or list(config.keys())
    ranked = find_best_agent(args.task, agents, config)
    print(f"task: {args.task}", flush=True)
    print(f"best agents:", flush=True)
    for i, name in enumerate(ranked):
        info = config.get(name, {})
        print(f"  {i+1}. {name} - {info.get('description', '')}", flush=True)
    return 0


@command
def cmd_smart(args: argparse.Namespace, config: dict) -> int:
    base_dir = args.dir or os.path.join(os.getcwd(), ".mew")
    src = os.getcwd()

    print(f"task: {args.task}", flush=True)
    print("router: Graphify -> Claude complexity -> Headroom -> OmniRoute -> Codex", flush=True)
    print(f"omniroute: {OMNIROUTE_BASE_URL}", flush=True)
    print(f"isolate: {args.isolate}", flush=True)

    if args.dry_run:
        print("[mew] dry-run: classifying task only", flush=True)

    Path(base_dir).mkdir(parents=True, exist_ok=True)
    workdir = src
    branch = ""
    rid = run_id()
    if args.isolate:
        workdir = os.path.join(base_dir, "smart")
        ok, err = add_worktree(src, workdir, f"mew/{rid}/smart")
        branch = f"mew/{rid}/smart"
        if not ok:
            print(f"[mew] smart worktree failed: {err}", file=sys.stderr, flush=True)
            return 1

    try:
        decision = classify_task(
            args.task, config, workdir, args.timeout, args.yolo,
            allow_fallback=getattr(args, "allow_classifier_fallback", False),
        )
        if "error" in decision:
            classifier = decision.get("classifier", {})
            detail = (classifier.get("error") or classifier.get("output") or "").strip()
            print(f"[mew] {decision['error']}", file=sys.stderr, flush=True)
            if detail:
                print(f"[mew] classifier detail: {detail}", file=sys.stderr, flush=True)
            return 1
        complexity = decision["complexity"]
        model = args.model or COMPLEXITY_MODELS[complexity]
        print(f"[mew] complexity: {complexity} ({decision['reason']})", flush=True)
        print(f"[mew] model: {model}", flush=True)

        if args.dry_run:
            return 0

        src_before = git_status(src, _ignore_prefix(src, base_dir))
        smart_config = dict(config)
        smart_config["omniroute_codex"] = {
            "description": "Codex via Headroom, forwarding OpenAI-compatible traffic to OmniRoute.",
            "strengths": ["smart", "omniroute", "coding", "implementation"],
            "cmd": ["codex", "exec", "--model", model],
            "yolo_args": DEFAULT_AGENTS["codex"]["yolo_args"],
            "headroom_target": "codex",
        }
        result = run_agent("omniroute_codex", args.task, smart_config, workdir, args.timeout, args.yolo, args.headroom, args.graphify)
        result["complexity"] = complexity
        result["selected_model"] = model
        result["classifier_reason"] = decision["reason"]
        results = [_audit(result, src, src_before, args.isolate, _ignore_prefix(src, base_dir))]
        output = format_json(results) if args.json else format_results(results, mode="smart")
        print(output, flush=True)
        return 0 if result.get("status") == "completed" else 1
    finally:
        if args.isolate:
            remove_worktree(workdir)
            if branch:
                subprocess.run(["git", "branch", "-D", branch], capture_output=True, cwd=src)


@command
def cmd_profile(args: argparse.Namespace, config: dict) -> int:
    print("agent profiles:", flush=True)
    for name, info in config.items():
        print(f"", flush=True)
        print(f"  [{name}]", flush=True)
        print(f"  desc: {info.get('description', '')}", flush=True)
        print(f"  cmd:  {' '.join(info.get('cmd', []))} [...]", flush=True)
        print(f"  strengths: {', '.join(info.get('strengths', []))}", flush=True)
    return 0


@command
def cmd_plan(args: argparse.Namespace, config: dict) -> int:
    planner = args.planner
    available = args.agents or list(config.keys())
    base_dir = args.dir or os.path.join(os.getcwd(), ".mew")

    agent_lines = []
    for name in available:
        info = config[name]
        desc = info.get("description", "")
        strengths = ", ".join(info.get("strengths", []))
        agent_lines.append(f"  [{name}] {desc}")
        agent_lines.append(f"       strengths: {strengths}")

    planner_prompt = PLANNER_PROMPT.format(
        profiles="\n".join(agent_lines),
        task=args.task,
        chaining=CHAINED_RULES if args.chain else UNCHAINED_RULES,
    )

    Path(base_dir).mkdir(parents=True, exist_ok=True)
    print(f"[mew] planning with {planner}...", flush=True)
    plan_result = run_agent(planner, planner_prompt, config, base_dir, args.timeout, args.yolo, args.headroom, args.graphify)

    if plan_result["status"] != "completed":
        print(f"[mew] planner failed: {plan_result['error']}", file=sys.stderr, flush=True)
        return 1

    plan = extract_json(plan_result["output"])
    if not plan:
        print("[mew] could not parse plan from planner output", file=sys.stderr, flush=True)
        return 1

    errors = []
    for i, step in enumerate(plan):
        if "agent" not in step or "task" not in step:
            errors.append(f"  step {i+1}: missing 'agent' or 'task' key")
        elif step["agent"] not in config:
            errors.append(f"  step {i+1}: unknown agent '{step['agent']}'")
    if errors:
        for e in errors:
            print(e, file=sys.stderr, flush=True)
        return 1

    print(f"[mew] plan: {len(plan)} steps", flush=True)
    for i, step in enumerate(plan):
        print(f"  {i+1}. [{step['agent']}] {step['task']}", flush=True)

    warnings = plan_warnings(plan, args.chain)
    if warnings:
        print("[mew] plan warnings:", file=sys.stderr, flush=True)
        for w in warnings:
            print(w, file=sys.stderr, flush=True)

    if args.dry_run:
        print("[mew] dry-run — skipping execution", flush=True)
        return 0

    results = plan_dispatch(plan, config, base_dir, args.timeout, args.yolo, chain=args.chain, isolate=args.isolate, headroom=args.headroom, graphify=args.graphify)
    output = format_json(results) if args.json else format_results(results, mode="plan")
    print(output, flush=True)
    return 0


@command
def cmd_tui(args: argparse.Namespace, config: dict) -> int:
    try:
        from mewmew_orchestrator.tui import run_tui
    except ImportError:
        print(
            "mew TUI requires textual.\n"
            "Install with: pip install mewmew-orchestrator[tui]",
            file=sys.stderr,
        )
        return 1
    run_tui(config=config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mew",
        description="Multi-agent orchestrator for AI coding CLI agents (codex, opencode, agy, etc.)",
    )
    parser.add_argument("--config", "-c", type=str, help="Path to agent config YAML file")

    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run agents in parallel on the same task")
    _shared_args(run_p)
    run_p.add_argument("prompt", type=str, help="Task description")
    run_p.add_argument("--agents", nargs="+", default=["all"], help="Agent names (default: all)")
    run_p.add_argument("--dry-run", action="store_true", help="Print plan without executing")

    pipe_p = sub.add_parser("pipeline", help="Run agents sequentially in a pipeline")
    _shared_args(pipe_p)
    pipe_p.add_argument("--assign", nargs="+", required=True, help="Agent names in pipeline order")
    pipe_p.add_argument("--tasks", nargs="+", required=True, help="One task per agent in matching order")
    pipe_p.add_argument("--dry-run", action="store_true", help="Print plan without executing")
    pipe_p.add_argument(
        "--chain", action=argparse.BooleanOptionalAction, default=True,
        help="Run all steps in one shared worktree so later steps see earlier files (--no-chain for one worktree per step)",
    )

    sub.add_parser("profile", help="Show agent capability profiles")
    sub.add_parser("config", help="Print the default agent config to stdout")
    cmd_clean_parser(sub)

    route_p = sub.add_parser("route", help="Find the best agent for a task")
    route_p.add_argument("task", type=str, help="Task description")
    route_p.add_argument("--agents", nargs="+", help="Candidates (default: all)")

    smart_p = sub.add_parser("smart", help="Graphify + Claude complexity routing through Headroom and OmniRoute")
    _shared_args(smart_p)
    smart_p.add_argument("task", type=str, help="Task description")
    smart_p.add_argument("--model", type=str, help="Override the OmniRoute model alias selected by Claude complexity")
    smart_p.add_argument(
        "--allow-classifier-fallback", action="store_true",
        help="Route as moderate if Claude classification fails instead of failing closed",
    )
    smart_p.add_argument("--dry-run", action="store_true", help="Classify and print selected model without executing")

    plan_p = sub.add_parser("plan", help="Decompose task and dispatch sub-tasks to best-fit agents")
    _shared_args(plan_p)
    plan_p.add_argument("task", type=str, help="High-level task description")
    plan_p.add_argument("--planner", type=str, default="opencode", help="Agent used to create the plan (default: opencode)")
    plan_p.add_argument("--agents", nargs="+", help="Agents available for sub-tasks (default: all)")
    plan_p.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    plan_p.add_argument(
        "--chain", action=argparse.BooleanOptionalAction, default=True,
        help="Run steps in one shared worktree (--no-chain for one worktree per step, which forces independent steps only)",
    )

    sub.add_parser("tui", help="Launch the terminal UI (textual)")

    return parser


def _shared_args(sub):
    sub.add_argument("--yolo", action="store_true", help="Auto-approve all tool permissions")
    sub.add_argument("--timeout", type=int, default=600, help="Per-agent timeout (seconds)")
    sub.add_argument("--dir", type=str, help="Working directory for agent outputs")
    sub.add_argument("--json", action="store_true", help="Output results as JSON")
    sub.add_argument(
        "--isolate", action=argparse.BooleanOptionalAction, default=True,
        help="Isolate agents in git worktrees (--no-isolate to run in place)",
    )
    sub.add_argument(
        "--no-graphify", dest="graphify", action="store_false", default=True,
        help="Skip the graphify knowledge-graph preflight. It is a hard gate by "
             "default and aborts the run when the graph comes out empty.",
    )
    sub.add_argument(
        "--no-headroom", dest="headroom", action="store_false", default=True,
        help="Call the agent CLI directly instead of routing it through the Headroom "
             "cost proxy. Use this when the proxy's upstream is down or returns 401, "
             "which otherwise makes every agent fail without writing anything.",
    )


def cmd_clean_parser(sub):
    p = sub.add_parser("clean", help="Remove .mew worktrees and mew/* branches")
    p.add_argument("--dir", type=str, help="Working directory to clean (default .mew)")
    return p


@command
def cmd_config(args: argparse.Namespace, config: dict) -> int:
    print("# mew agent configuration")
    print("# save this as mew.yaml or pass --config /path/to/mew.yaml")
    print()
    try:
        import yaml as _yaml
        print(_yaml.dump(DEFAULT_AGENTS, default_flow_style=False))
    except ImportError:
        print("(install PyYAML for YAML output)")
        import json
        print(json.dumps(DEFAULT_AGENTS, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = args.config or os.environ.get("MEW_CONFIG")
    config = load_config(config_path)

    fn = COMMANDS.get(args.command)
    if fn:
        return fn(args, config)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
