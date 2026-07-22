"""
mew - Multi-agent orchestrator for codex, opencode, and agy.

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
    },
    "opencode": {
        "description": "Anthropic OpenCode. Architecture, review, refactoring, testing, docs.",
        "strengths": ["architecture", "review", "refactor", "test", "document", "plan"],
        "cmd": ["opencode", "run"],
    },
    "agy": {
        "description": "Google Antigravity CLI. GCP, Android, Kotlin, Go, cloud infra.",
        "strengths": ["gcp", "android", "kotlin", "go", "cloud", "kubernetes", "deploy"],
        "cmd": ["agy", "--prompt"],
    },
}


def load_config(path: str | None = None) -> dict:
    config = DEFAULT_AGENTS.copy()
    if path:
        if yaml is None:
            print("warning: PyYAML not installed, using default config", file=sys.stderr)
            return config
        try:
            with open(path) as f:
                overrides = yaml.safe_load(f) or {}
            config.update(overrides.get("agents", {}))
        except FileNotFoundError:
            print(f"warning: config {path} not found, using defaults", file=sys.stderr)
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


def run_agent(name: str, prompt: str, config: dict, workdir: str, timeout: int, yolo: bool) -> dict[str, Any]:
    agent = config.get(name)
    if not agent:
        return {"agent": name, "status": "error", "error": f"unknown agent: {name}", "workdir": workdir}

    result: dict[str, Any] = {
        "agent": name, "status": "running", "output": "", "error": "",
        "workdir": workdir, "returncode": -1,
    }
    cmd = list(agent["cmd"]) + [prompt]

    if name == "agy":
        cmd.extend(["--add-dir", workdir])

    if yolo:
        if name == "codex":
            cmd.extend(["-c", "sandbox_permissions=allow-all"])
        elif name == "agy":
            cmd.append("--dangerously-skip-permissions")

    env = os.environ.copy()

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

        result["status"] = "completed" if proc.returncode == 0 else "failed"
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


def prepare_workdirs(base_dir: str, agents: list[str]) -> dict[str, str]:
    dirs: dict[str, str] = {}
    shutil.rmtree(base_dir, ignore_errors=True)
    Path(base_dir).mkdir(parents=True, exist_ok=True)
    src = os.getcwd()

    in_git = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True).returncode == 0

    for name in agents:
        agent_dir = os.path.join(base_dir, f"agent-{name}")
        if in_git and shutil.which("git"):
            branch = f"mew-{name}"
            wt_path = os.path.join(base_dir, f"wt-{name}")
            r = subprocess.run(
                ["git", "worktree", "add", "-b", branch, wt_path],
                capture_output=True, text=True, cwd=src,
            )
            if r.returncode == 0:
                dirs[name] = wt_path
                continue
            shutil.rmtree(wt_path, ignore_errors=True)
        if os.path.exists(agent_dir):
            shutil.rmtree(agent_dir)
        shutil.copytree(src, agent_dir, ignore=shutil.ignore_patterns(".git", "node_modules", ".mew"))
        dirs[name] = agent_dir
    return dirs


def cleanup_workdirs(base_dir: str, agents: list[str]) -> None:
    for name in agents:
        wt_path = os.path.join(base_dir, f"wt-{name}")
        r = subprocess.run(
            ["git", "worktree", "remove", "--force", wt_path],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            shutil.rmtree(wt_path, ignore_errors=True)


def parallel_dispatch(prompt: str, agents: list[str], config: dict, base_dir: str, timeout: int, yolo: bool, isolate: bool) -> list[dict]:
    results: list[dict] = []
    threads: list[threading.Thread] = []
    lock = threading.Lock()

    if isolate:
        dirs = prepare_workdirs(base_dir, agents)
    else:
        Path(base_dir).mkdir(parents=True, exist_ok=True)
        dirs = {name: base_dir for name in agents}

    def worker(name: str):
        r = run_agent(name, prompt, config, dirs[name], timeout, yolo)
        with lock:
            results.append(r)

    for name in agents:
        t = threading.Thread(target=worker, args=(name,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    if isolate:
        cleanup_workdirs(base_dir, agents)

    return results


def pipeline_dispatch(steps: list[tuple[str, str]], config: dict, base_dir: str, timeout: int, yolo: bool) -> list[dict]:
    results: list[dict] = []
    shutil.rmtree(base_dir, ignore_errors=True)
    Path(base_dir).mkdir(parents=True, exist_ok=True)
    context = ""
    src = os.getcwd()

    in_git = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True).returncode == 0

    for i, (agent_name, task) in enumerate(steps):
        step_dir = os.path.join(base_dir, f"step-{i+1}-{agent_name}")
        prompt = f"{task}\n\n<context>\n{context[:3000]}\n</context>" if context else task

        if in_git and shutil.which("git"):
            branch = f"mew-pipe-step{i+1}"
            r = subprocess.run(
                ["git", "worktree", "add", "-b", branch, step_dir],
                capture_output=True, text=True, cwd=src,
            )
            if r.returncode != 0:
                shutil.rmtree(step_dir, ignore_errors=True)
                shutil.copytree(src, step_dir, ignore=shutil.ignore_patterns(".git", "node_modules", ".mew"))
        else:
            shutil.copytree(src, step_dir, ignore=shutil.ignore_patterns(".git", "node_modules", ".mew"))

        r = run_agent(agent_name, prompt, config, step_dir, timeout, yolo)
        results.append(r)
        context += f"\n--- Step {i+1} ({agent_name}) ---\n{r['output'][:3000]}"

    return results


PLANNER_PROMPT = """\
You are a planner that breaks development tasks into clear sub-tasks and assigns each to the best agent.

Available agents:
{profiles}

Rules:
- Each sub-task targets ONE agent — pick the agent whose strengths fit best
- Order sub-tasks so later steps can build on earlier ones (dependencies first)
- Keep sub-tasks focused and actionable

Respond with ONLY a valid JSON array. No markdown, no explanation, no code blocks:
[
  {{"agent": "<agent_name>", "task": "<concise sub-task description>"}}
]

Task: {task}"""


def plan_dispatch(plan: list[dict], config: dict, base_dir: str, timeout: int, yolo: bool) -> list[dict]:
    results: list[dict] = []
    shutil.rmtree(base_dir, ignore_errors=True)
    Path(base_dir).mkdir(parents=True, exist_ok=True)
    context = ""
    src = os.getcwd()

    in_git = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True).returncode == 0

    for i, step in enumerate(plan):
        agent_name = step["agent"]
        task = step["task"]
        step_dir = os.path.join(base_dir, f"step-{i+1}-{agent_name}")
        prompt = f"{task}\n\n<context>\n{context[:3000]}\n</context>" if context else task

        if in_git and shutil.which("git"):
            branch = f"mew-plan-step{i+1}"
            r = subprocess.run(
                ["git", "worktree", "add", "-b", branch, step_dir],
                capture_output=True, text=True, cwd=src,
            )
            if r.returncode != 0:
                shutil.rmtree(step_dir, ignore_errors=True)
                shutil.copytree(src, step_dir, ignore=shutil.ignore_patterns(".git", "node_modules", ".mew"))
        else:
            shutil.copytree(src, step_dir, ignore=shutil.ignore_patterns(".git", "node_modules", ".mew"))

        r = run_agent(agent_name, prompt, config, step_dir, timeout, yolo)
        results.append(r)
        context += f"\n--- Step {i+1} ({agent_name}) ---\n{r['output'][:3000]}"

    return results


def format_results(results: list[dict], mode: str = "run") -> str:
    lines = [f"[mew] mode: {mode.upper()}", f"[mew] agents: {len(results)}"]

    for i, r in enumerate(results):
        lines.extend([
            "",
            f"{'='*60}",
            f"  agent #{i+1}: {r['agent']}",
            f"  status:     {r['status']}",
            f"  returncode: {r['returncode']}",
            f"{'='*60}",
        ])
        output = (r.get("output") or "").strip()
        error = (r.get("error") or "").strip()
        if output:
            lines.append(output)
        if error:
            lines.append(f"[stderr]\n{error}")

    lines.append(f"\n{'='*60}")
    lines.append("[mew] done")
    return "\n".join(lines)


def format_json(results: list[dict]) -> str:
    return json.dumps(results, indent=2)


COMMANDS = {}


def command(fn):
    COMMANDS[fn.__name__.replace("cmd_", "")] = fn
    return fn


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

    results = parallel_dispatch(args.prompt, agents, config, base_dir, args.timeout, args.yolo, args.isolate)
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

    if args.dry_run:
        print("[dry-run] skipping execution", flush=True)
        return 0

    results = pipeline_dispatch(steps, config, base_dir, args.timeout, args.yolo)
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
    )

    Path(base_dir).mkdir(parents=True, exist_ok=True)
    print(f"[mew] planning with {planner}...", flush=True)
    plan_result = run_agent(planner, planner_prompt, config, base_dir, args.timeout, args.yolo)

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

    if args.dry_run:
        print("[mew] dry-run — skipping execution", flush=True)
        return 0

    results = plan_dispatch(plan, config, base_dir, args.timeout, args.yolo)
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

    sub.add_parser("profile", help="Show agent capability profiles")
    sub.add_parser("config", help="Print the default agent config to stdout")

    route_p = sub.add_parser("route", help="Find the best agent for a task")
    route_p.add_argument("task", type=str, help="Task description")
    route_p.add_argument("--agents", nargs="+", help="Candidates (default: all)")

    plan_p = sub.add_parser("plan", help="Decompose task and dispatch sub-tasks to best-fit agents")
    _shared_args(plan_p)
    plan_p.add_argument("task", type=str, help="High-level task description")
    plan_p.add_argument("--planner", type=str, default="opencode", help="Agent used to create the plan (default: opencode)")
    plan_p.add_argument("--agents", nargs="+", help="Agents available for sub-tasks (default: all)")
    plan_p.add_argument("--dry-run", action="store_true", help="Show plan without executing")

    sub.add_parser("tui", help="Launch the terminal UI (textual)")

    return parser


def _shared_args(sub):
    sub.add_argument("--yolo", action="store_true", help="Auto-approve all tool permissions")
    sub.add_argument("--timeout", type=int, default=600, help="Per-agent timeout (seconds)")
    sub.add_argument("--dir", type=str, help="Working directory for agent outputs")
    sub.add_argument("--json", action="store_true", help="Output results as JSON")
    sub.add_argument("--isolate", action="store_true", default=True, help="Isolate agents via git worktree")


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
