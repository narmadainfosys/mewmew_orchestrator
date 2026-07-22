"""
mew TUI - Textual-based multi-agent orchestrator.
"""

from __future__ import annotations

import asyncio
import os
import sys

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal
    from textual.widgets import Header, Input, RichLog, Static
except ImportError:
    print(
        "mew TUI needs textual: pip install mewmew-orchestrator[tui]",
        file=sys.stderr,
    )
    sys.exit(1)

from mewmew_orchestrator.cli import DEFAULT_AGENTS


class AgentPane(Static):
    """A vertical pane showing one agent's real-time output."""

    def __init__(self, name: str, desc: str, **kwargs):
        super().__init__(**kwargs)
        self.agent_name = name
        self.agent_desc = desc

    def compose(self):
        yield Static(
            f"[bold white]{self.agent_name}[/bold white]  [dim]{self.agent_desc}[/dim]",
            classes="agent-title",
        )
        yield RichLog(id=f"log-{self.agent_name}", max_lines=10000, wrap=True)

    @property
    def log(self) -> RichLog:
        return self.query_one(RichLog)

    def write_line(self, text: str):
        self.log.write(text)

    def clear_log(self):
        self.log.clear()


class MewApp(App):
    """mew orchestrator TUI."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #pane-row {
        height: 3fr;
    }

    AgentPane {
        width: 1fr;
        border: solid $primary 30%;
    }

    AgentPane > .agent-title {
        text-align: center;
        background: $surface;
        padding: 1;
        border-bottom: solid $primary;
    }

    RichLog {
        height: 1fr;
        padding: 0 1;
    }

    #chat-area {
        height: 2fr;
        border: solid $secondary;
        padding: 1;
    }

    #input-area {
        dock: bottom;
        height: 3;
        padding: 0 1;
    }

    Input {
        width: 100%;
    }
    """

    def __init__(self, config: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self.config = config or DEFAULT_AGENTS
        self.agent_names = list(self.config.keys())[:3]
        self._tasks: dict[str, asyncio.Task] = {}

    def compose(self):
        yield Header()
        with Horizontal(id="pane-row"):
            for name in self.agent_names:
                info = self.config[name]
                yield AgentPane(name, info.get("description", ""))
        yield Static(id="chat-area")
        yield Input(placeholder="Type a task and press Enter…", id="input-area")

    def on_mount(self):
        for name in self.agent_names:
            self.query_one(f"#log-{name}", RichLog).write("[dim]ready[/dim]")
        self.query_one("#chat-area", Static).update(
            "[dim]type a task below to run all agents in parallel[/dim]"
        )

    def on_input_submitted(self, event: Input.Submitted):
        task = event.value.strip()
        if not task:
            return
        event.input.clear()

        for t in self._tasks.values():
            t.cancel()
        self._tasks.clear()

        self.query_one("#chat-area", Static).update(f"[bold yellow]▶[/bold yellow] {task}")

        for name in self.agent_names:
            self.query_one(f"#log-{name}", RichLog).clear()
            self._run_agent(name, task)

    def _run_agent(self, name: str, task: str):
        async def run():
            info = self.config[name]
            cmd = list(info["cmd"]) + [task]

            if name == "agy":
                cmd.extend(["--add-dir", os.getcwd()])

            env = os.environ.copy()
            log = self.query_one(f"#log-{name}", RichLog)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=os.getcwd(),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                log.write(f"[red]error: '{cmd[0]}' not found — is {name} installed?[/red]")
                return

            async def read_stream(stream, prefix=""):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace").rstrip()
                    log.write(text)

            await asyncio.gather(
                read_stream(proc.stdout),
                read_stream(proc.stderr),
            )

            await proc.wait()

        self._tasks[name] = asyncio.create_task(run())

    def key_q(self):
        self.exit()


def run_tui(config: dict | None = None):
    MewApp(config=config).run()
