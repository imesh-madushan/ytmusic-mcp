"""Test doubles for the setup wizard: scripted input, a recording runner, canned services."""

import io
from collections.abc import Iterator
from pathlib import Path

from ytmusic_mcp.setup.proxy import Check
from ytmusic_mcp.setup.system import Result, Runner
from ytmusic_mcp.setup.ui import Console
from ytmusic_mcp.setup.wizard import Options, Services

CADDYFILE = """http://100.64.0.10:8080 {
\tbind 100.64.0.10
\treverse_proxy 127.0.0.1:8080
}

other.example.com {
\troot * /srv/files
\tbasicauth {
\t\tfileuser $2a$14$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ0123
\t}
\tfile_server browse
}
"""
SS_CADDY = (
    "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
    'LISTEN 0 4096 *:80 *:* users:(("caddy",pid=1146,fd=7))\n'
    'LISTEN 0 4096 *:443 *:* users:(("caddy",pid=1146,fd=10))\n'
)
SS_EMPTY = "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
PLAIN_SS_BEFORE = "LISTEN 0 4096 *:80 *:*\nLISTEN 0 4096 *:443 *:*\n"


def make_console(
    answers: list[str] | None = None, secrets: list[str] | None = None
) -> tuple[Console, io.StringIO]:
    """A console that reads scripted answers and writes to a buffer."""
    out = io.StringIO()
    typed: Iterator[str] = iter(answers or [])
    hidden: Iterator[str] = iter(secrets or [])

    def read(prompt: str) -> str:
        out.write(prompt + "\n")
        try:
            return next(typed)
        except StopIteration:
            raise AssertionError(f"The wizard asked an unexpected question: {prompt!r}") from None

    def read_secret(prompt: str) -> str:
        out.write(prompt + "\n")
        try:
            return next(hidden)
        except StopIteration:
            raise AssertionError(f"The wizard asked for an unexpected secret: {prompt!r}") from None

    return Console(out=out, read=read, read_secret=read_secret, color=False, unicode=True, width=70), out


class FakeRunner(Runner):
    """Records every command instead of running it. `responses` maps an argv prefix to a Result."""

    def __init__(
        self,
        console: Console,
        dry_run: bool = False,
        responses=None,
        files=None,
        tools=("systemctl", "caddy", "apt-get"),
    ):
        super().__init__(console, dry_run)
        self.calls: list[tuple[str, ...]] = []
        self.installed: dict[str, str] = {}
        self.install_order: list[str] = []
        self.responses = dict(responses or {})
        self.files = dict(files or {})
        self.tools = set(tools)

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.tools else None

    def read_file(self, path: str) -> str | None:
        return self.files.get(path)

    def run(self, argv, *, sudo=False, read_only=False, interactive=False, timeout=120) -> Result:
        self.calls.append(tuple(argv))
        if not read_only:
            self.console.command(" ".join(argv))
            if self.dry_run:
                return Result()
        for prefix, result in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return result() if callable(result) else result
        return Result(stdout="active\n" if argv[:2] == ["systemctl", "is-active"] else "")

    def install_file(self, text: str, destination: str, mode: str = "0644") -> Result:
        self.calls.append(("install", destination))
        if self.dry_run:
            return Result()
        self.installed[destination] = text
        self.install_order.append(destination)
        return Result()

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == prefix for call in self.calls)


def healthy_services(**overrides) -> Services:
    defaults = {
        "check_public": lambda domain: [Check("MCP endpoint asks for an OAuth token", True, "HTTP 401")],
        "check_local": lambda port: Check("App answers on this machine", True, "HTTP 200"),
        "sleep": lambda seconds: None,
        "system": lambda: "Linux",
        "public_ip": lambda: "203.0.113.5",
        "resolve": lambda domain: ["203.0.113.5"],
        "password_from_env": lambda: None,
    }
    return Services(**{**defaults, **overrides})


def make_options(project: Path, **overrides) -> Options:
    (project / ".env.example").write_text(
        (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return Options(project_dir=project, **overrides)
