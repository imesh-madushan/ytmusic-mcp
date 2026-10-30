"""The guided setup: from a fresh clone to a running, HTTPS-protected MCP server.

    python -m ytmusic_mcp.cli setup

It asks a few questions, shows what it is about to do, and then does it step by
step. Anything that touches the system outside this project (the web server
config, systemd) is shown first, backed up where it replaces something, and
undone if it fails. `--dry-run` shows everything without changing anything.
"""

import getpass
import os
import platform
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PROJECT_DIR, ConfigError
from ..errors import AccountError
from ..headers import encrypt_headers, headers_from_text
from ..security import CookieVault, generate_secret_key, hash_password
from ..store import Store
from . import proxy
from .envfile import read_env, set_env
from .proxy import CADDYFILE, Check, StepFailed
from .system import (
    NETWORK_FILESYSTEMS,
    Runner,
    filesystem_of,
    memory_mb,
    parse_listeners,
    public_ip,
    read_text,
    resolve_ipv4,
)
from .ui import Console

TOTAL_STEPS = 6
SERVICE_NAME = "ytmusic-mcp"
UNIT_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
PROXY_NAMES = {"caddy": "Caddy", "nginx": "nginx", "none": "no web server yet", "other": "another web server"}
DOMAIN_PATTERN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{2,32}$")


class SetupError(Exception):
    """The wizard cannot continue (bad input in non-interactive mode, a failed step)."""


@dataclass
class Options:
    domain: str | None = None
    username: str | None = None
    proxy: str = "auto"  # auto | caddy | nginx | none
    port: int | None = None
    yes: bool = False  # take the defaults without asking (needs --domain and --username)
    dry_run: bool = False
    replace_site: bool | None = None  # allow replacing an existing Caddy site for the domain
    skip_service: bool = False
    caddyfile: str = CADDYFILE
    project_dir: Path = field(default_factory=lambda: PROJECT_DIR)


@dataclass
class Services:
    """Things the wizard calls out to; replaceable in tests."""

    check_public: Callable[[str], list[Check]] = proxy.check_public
    check_local: Callable[[int], Check] = proxy.check_local
    sleep: Callable[[float], None] = time.sleep
    system: Callable[[], str] = platform.system
    public_ip: Callable[[], str | None] = public_ip
    resolve: Callable[[str], list[str]] = resolve_ipv4
    password_from_env: Callable[[], str | None] = lambda: os.environ.get("YTMUSIC_SETUP_PASSWORD")


# -- input checks ----------------------------------------------------------------------------


def validate_domain(value: str) -> str | None:
    if not DOMAIN_PATTERN.match(value.lower()):
        return "Enter a host name like mcp.example.com (no https:// and no path)."
    return None


def validate_username(value: str) -> str | None:
    if not USERNAME_PATTERN.match(value):
        return "Use 2-32 letters, digits, dots, dashes or underscores."
    return None


def validate_password(value: str) -> str | None:
    return None if len(value) >= 12 else "Use at least 12 characters."


def detect_proxy(listeners: dict[int, str]) -> str:
    """Which web server already owns the public ports."""
    owner = listeners.get(443) or listeners.get(80)
    if owner is None:
        return "none"
    name = owner.lower()
    return "caddy" if "caddy" in name else "nginx" if "nginx" in name else "other"


# -- the wizard ---------------------------------------------------------------------------------


class Wizard:
    def __init__(self, options: Options, console: Console, runner: Runner, services: Services | None = None):
        self.o = options
        self.ui = console
        self.run = runner
        self.run.dry_run = options.dry_run or runner.dry_run  # one switch for everything
        self.svc = services or Services()
        self.env_path = options.project_dir / ".env"
        self.domain = ""
        self.username = ""
        self.password = ""
        self.port = options.port or 8787
        self.proxy_kind = options.proxy
        self.listeners: dict[int, str] = {}
        self.secret_key = ""
        self.backup = ""
        self.proxy_done = False
        self.service_ok = False

    # entry point ------------------------------------------------------------------------------

    def execute(self) -> int:
        self.ui.header("YouTube Music MCP  ·  Setup", "A guided install for your own server")
        if self.o.dry_run:
            self.ui.banner("Dry run: nothing will be changed", good=False)
        try:
            self.step_check()
            self.step_details()
            self.step_configure()
            self.step_webserver()
            self.step_service()
            return self.step_finish()
        except (SetupError, StepFailed, ConfigError) as error:
            self.ui.line()
            self.ui.fail(str(error))
            self.ui.note(
                "Nothing further was changed. Fix the problem above and run the setup again; it is safe to re-run."
            )
            return 1
        except (KeyboardInterrupt, EOFError):
            self.ui.line()
            self.ui.warn("Setup cancelled. Anything already done is kept; run it again to continue.")
            return 130

    # step 1 -----------------------------------------------------------------------------------

    def step_check(self) -> None:
        self.ui.step(1, TOTAL_STEPS, "Checking this machine")
        if (system := self.svc.system()) != "Linux":
            raise SetupError(
                f"This guided setup is for Linux servers (this is {system}). For local use with Claude Desktop, "
                "run `python -m ytmusic_mcp.cli local-cookies` and see the README (stdio mode)."
            )
        version = sys.version_info
        if version < (3, 10):
            raise SetupError(f"Python 3.10 or newer is required (found {version.major}.{version.minor}).")
        self.ui.ok(f"Python {version.major}.{version.minor}")

        total, swap = memory_mb(read_text("/proc/meminfo"))
        if total:
            if total < 1500 and swap == 0:
                self.ui.warn(
                    f"{total} MB of memory and no swap. The app is small, but a swap file is a cheap safety net."
                )
            else:
                self.ui.ok(f"{total} MB memory" + (f", {swap} MB swap" if swap else ""))

        self.env = read_env(self.env_path)
        filesystem = filesystem_of(self._db_path().parent.resolve(), read_text("/proc/mounts"))
        if filesystem in NETWORK_FILESYSTEMS:
            self.ui.warn(
                f"The database folder is on a network disk ({filesystem}). SQLite can corrupt there;"
            )
            self.ui.note("set YTMUSIC_DB_PATH in .env to a folder on the local disk, then re-run.")
        elif filesystem:
            self.ui.ok(f"Database folder is on a local disk ({filesystem})")

        if self.run.which("systemctl"):
            self.ui.ok("systemd is available")
        else:
            self.ui.warn("systemd was not found; the service step will be skipped.")
            self.o.skip_service = True

        result = self.run.run(["ss", "-tlnp"], sudo=True, read_only=True)
        self.listeners = parse_listeners(result.stdout)
        if self.proxy_kind == "auto":
            self.proxy_kind = detect_proxy(self.listeners)
            owner = self.listeners.get(443) or self.listeners.get(80) or ""
            detail = f" ({owner} is using ports 80/443)" if owner else ""
            self.ui.ok(f"Web server: {PROXY_NAMES[self.proxy_kind]}{detail}")
        else:
            self.ui.ok(f"Web server: {PROXY_NAMES.get(self.proxy_kind, self.proxy_kind)} (chosen)")

    # step 2 -----------------------------------------------------------------------------------

    def step_details(self) -> None:
        self.ui.step(2, TOTAL_STEPS, "Your details")
        existing_url = self.env.get("MCP_PUBLIC_URL", "")
        existing_domain = (
            re.sub(r"^https?://([^/]+).*$", r"\1", existing_url) if "yourdomain" not in existing_url else ""
        )
        if self.o.yes:
            self.domain = (self.o.domain or existing_domain).lower()
            self.username = self.o.username or ""
            self.password = self.svc.password_from_env() or ""
            for label, value, check in (
                ("--domain", self.domain, validate_domain),
                ("--username", self.username, validate_username),
                ("YTMUSIC_SETUP_PASSWORD", self.password, validate_password),
            ):
                problem = check(value) if value else "is required with --yes"
                if problem:
                    raise SetupError(f"{label}: {problem}")
        else:
            self.domain = self.o.domain or self.ui.ask(
                "Domain for the server",
                default=existing_domain or None,
                validate=validate_domain,
                hint="A name that already points at this server, e.g. mcp.example.com",
            )
            self.domain = self.domain.lower()
            self.username = self.o.username or self.ui.ask(
                "Username for signing in", validate=validate_username
            )
            self.password = self._ask_password()
        self._check_dns()

    def _ask_password(self) -> str:
        if self.o.dry_run:
            return "dry-run-password"
        if self._user_exists():
            self.ui.note(f"'{self.username}' already exists; leave this empty to keep the current password.")
            first = self.ui.secret("New password (optional)", lambda value: None)
            if not first:
                return ""
            self.ui.secret(
                "Repeat password", lambda value: None if value == first else "Passwords do not match."
            )
            return first
        while True:
            first = self.ui.secret("Password (12+ characters)", validate_password)
            if self.ui.secret("Repeat password") == first:
                return first
            self.ui.fail("Passwords do not match.")

    def _user_exists(self) -> bool:
        path = self._db_path()
        return path.exists() and Store(path).get_user(self.username) is not None

    def _check_dns(self) -> None:
        addresses = self.svc.resolve(self.domain)
        mine = self.svc.public_ip()
        if not addresses:
            self.ui.warn(
                f"{self.domain} does not resolve yet. Add a DNS A record for it before the HTTPS step."
            )
        elif mine and mine not in addresses:
            self.ui.warn(
                f"{self.domain} points at {', '.join(addresses)}, but this server's address is {mine}."
            )
        else:
            self.ui.ok(f"{self.domain} resolves to {', '.join(addresses)}")

    # step 3 -----------------------------------------------------------------------------------

    def _db_path(self) -> Path:
        merged = {**self.env, **getattr(self, "planned_env", {})}
        merged.setdefault("YTMUSIC_DB_PATH", str(self.o.project_dir / "data" / "ytmusic-mcp.db"))
        return Path(merged["YTMUSIC_DB_PATH"]).expanduser()

    def step_configure(self) -> None:
        self.ui.step(3, TOTAL_STEPS, "Configuring the app")
        key = self.env.get("YTMUSIC_SECRET_KEY") or self.env.get("YTMUSIC_CREDENTIAL_KEY") or ""
        new_key = not key or key == "REPLACE_ME"
        self.secret_key = generate_secret_key() if new_key else key
        updates = {
            "MCP_TRANSPORT": "http",
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(self.port or self.env.get("MCP_PORT", 8787)),
            "MCP_PUBLIC_URL": f"https://{self.domain}/ytmusic-mcp",
        }
        if new_key:
            updates["YTMUSIC_SECRET_KEY"] = self.secret_key
        if "OAUTH_DB_PATH" in self.env and "YTMUSIC_DB_PATH" not in self.env:
            updates["YTMUSIC_DB_PATH"] = str(self.o.project_dir / "data" / "ytmusic-mcp.db")
            self.ui.warn("An older OAUTH_DB_PATH setting was found; using a fresh database instead.")
        self.port = int(updates["MCP_PORT"])
        self.planned_env = updates

        self.ui.line()
        self.ui.table(
            [
                ("Server address", f"https://{self.domain}/ytmusic-mcp"),
                ("Sign-in user", self.username),
                ("Web server", PROXY_NAMES.get(self.proxy_kind, self.proxy_kind)),
                ("Service", "none (skipped)" if self.o.skip_service else f"systemd unit '{SERVICE_NAME}'"),
                ("Settings file", str(self.env_path)),
            ]
        )
        self.ui.line()
        if (
            not self.o.yes
            and not self.o.dry_run
            and not self.ui.confirm("Apply these changes?", default=True)
        ):
            raise SetupError("Cancelled before anything was changed.")

        if self.o.dry_run:
            self.ui.info(f"Would write {len(updates)} settings to {self.env_path}")
        else:
            set_env(self.env_path, updates, template=self.o.project_dir / ".env.example")
            self.ui.ok(f"Saved settings to {self.env_path} (readable only by you)")
        if new_key:
            self.ui.warn(
                "A new encryption key was created for stored cookies. Back up YTMUSIC_SECRET_KEY from .env;"
            )
            self.ui.note("if it is lost, everyone has to reconnect their YouTube account.")
        else:
            self.ui.ok("Kept your existing encryption key")

        self._create_user()

    def _create_user(self) -> None:
        if self.o.dry_run:
            self.ui.info(f"Would create user '{self.username}'")
            return
        db_path = self._db_path()
        (db_path.parent).mkdir(parents=True, exist_ok=True)
        store = Store(db_path)
        if store.get_user(self.username) is None:
            store.create_user(self.username, hash_password(self.password))
            self.ui.ok(f"Created user '{self.username}'")
        elif self.password:
            store.set_password(self.username, hash_password(self.password))
            self.ui.ok(f"Updated the password for '{self.username}'")
        else:
            self.ui.ok(f"Kept the existing user '{self.username}'")

        user = store.get_user(self.username)
        if user and user.cookies is None and not self.o.yes:
            self.ui.line()
            if self.ui.confirm(
                "Paste your YouTube headers now? (otherwise you are asked at first sign-in)", default=False
            ):
                self._save_cookies(store)

    def _save_cookies(self, store: Store) -> None:
        text = self.ui.multiline(
            "In music.youtube.com: F12 > Network > filter 'youtubei' > right-click a POST request > "
            "Copy > Copy as cURL (bash). Paste it here, then press Enter on an empty line."
        )
        try:
            encrypted = encrypt_headers(headers_from_text(text), CookieVault(self.secret_key))
        except AccountError as error:
            self.ui.warn(
                f"Those headers could not be used ({error}). You can add them when you first sign in."
            )
            return
        store.set_cookies(self.username, encrypted)
        self.ui.ok("YouTube headers saved (encrypted)")

    # step 4 -----------------------------------------------------------------------------------

    def step_webserver(self) -> None:
        self.ui.step(4, TOTAL_STEPS, "Web server and HTTPS")
        kind = self.proxy_kind
        if kind == "none":
            kind = self._offer_caddy()
        if kind == "caddy":
            self.backup = proxy.configure_caddy(
                self.run,
                self.ui,
                self.domain,
                self.port,
                replace_existing=self.o.replace_site,
                caddyfile=self.o.caddyfile,
            )
            self.proxy_done = True
        elif kind == "nginx":
            self.backup = proxy.configure_nginx(self.run, self.ui, self.domain, self.port)
            self.proxy_done = True
        else:
            self._show_manual_routes()

    def _show_manual_routes(self) -> None:
        """For a web server we do not configure: say exactly what it must forward."""
        upstream = f"127.0.0.1:{self.port}"
        self.ui.warn("This web server needs to be set up by hand. Forward these paths to the app:")
        self.ui.table(
            [
                ("/ytmusic-mcp", f"{upstream}/mcp  (rewrite the path to /mcp)"),
                ("these paths", f"{upstream}  (same path)"),
            ],
            indent=6,
        )
        for path in proxy.OAUTH_PATHS:
            self.ui.note(f"  {path}")
        self.ui.note("Answer 404 for everything else, and serve it over HTTPS with a valid certificate.")
        self.ui.note("For /ytmusic-mcp, turn off response buffering and allow long timeouts (it streams).")
        self.ui.note("Ready-made examples: deploy/caddy-site.template and deploy/nginx.conf.template")

    def _offer_caddy(self) -> str:
        """Nothing is serving ports 80/443: offer Caddy, which handles HTTPS certificates itself."""
        self.ui.info("No web server is using ports 80 and 443.")
        if not self.run.which("apt-get"):
            self.ui.warn("Install a web server (Caddy is simplest) and add the routes below.")
            return "other"
        if self.o.yes or self.ui.confirm(
            "Install Caddy? It gets and renews HTTPS certificates automatically", default=True
        ):
            if not self.run.run(["apt-get", "install", "-y", "caddy"], sudo=True, timeout=600).ok:
                raise StepFailed(
                    "Installing Caddy failed. Install a web server yourself and re-run the setup."
                )
            self.ui.ok("Caddy installed")
            return "caddy"
        return "other"

    # step 5 -----------------------------------------------------------------------------------

    def step_service(self) -> None:
        self.ui.step(5, TOTAL_STEPS, "Starting the service")
        if self.o.skip_service:
            self.ui.warn("Skipped. Start it yourself with: python -m ytmusic_mcp")
            return
        unit = (proxy.DEPLOY_DIR / "ytmusic-mcp.service.template").read_text(encoding="utf-8")
        data_dir = self._db_path().parent
        for placeholder, value in (
            ("@USER@", getpass.getuser()),
            ("@APP_DIR@", str(self.o.project_dir)),
            ("@DATA_DIR@", str(data_dir)),
        ):
            unit = unit.replace(placeholder, value)
        if not self.run.install_file(unit, UNIT_PATH).ok:
            raise StepFailed("Could not install the systemd unit.")
        for command in (
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", SERVICE_NAME],
            ["systemctl", "restart", SERVICE_NAME],
        ):
            if not self.run.run(command, sudo=True).ok:
                raise StepFailed(f"'{' '.join(command)}' failed. See: journalctl -u {SERVICE_NAME} -n 40")
        if self.o.dry_run:
            return
        for _ in range(10):  # the first start can take a few seconds
            check = self.svc.check_local(self.port)
            if check.ok:
                self.ui.ok(f"The app is running on this machine ({check.detail})")
                self.service_ok = True
                return
            self.svc.sleep(2)
        raise StepFailed(f"The service did not start. See: journalctl -u {SERVICE_NAME} -n 40 --no-pager")

    # step 6 -----------------------------------------------------------------------------------

    def step_finish(self) -> int:
        self.ui.step(6, TOTAL_STEPS, "Checking it from the outside")
        results: list[Check] = []
        if self.o.dry_run or not self.proxy_done:
            self.ui.info("Skipped (nothing is published yet).")
        else:
            for attempt in range(8):  # Caddy fetches the certificate on the first request
                results = self.svc.check_public(self.domain)
                if all(result.ok for result in results):
                    break
                if attempt == 0:
                    self.ui.note("Waiting for the HTTPS certificate…")
                self.svc.sleep(5)
            for result in results:
                (self.ui.ok if result.ok else self.ui.fail)(f"{result.name} ({result.detail})")
        healthy = bool(results) and all(result.ok for result in results)
        self._print_summary(healthy)
        return 0 if (healthy or self.o.dry_run or not self.proxy_done) else 1

    def _print_summary(self, healthy: bool) -> None:
        self.ui.banner("All set" if healthy else "Setup finished, with items to check", good=healthy)
        self.ui.line()
        rows = [
            ("Connector URL", f"https://{self.domain}/ytmusic-mcp"),
            ("Sign in as", self.username),
        ]
        if self.backup:
            rows.append(("Config backup", self.backup))
        rows.append(("Encryption key", f"{self.env_path} (back up YTMUSIC_SECRET_KEY)"))
        self.ui.table(rows)
        self.ui.line()
        self.ui.info("Next: connect your MCP client (for example Claude or ChatGPT)")
        for number, text in enumerate(
            (
                "Add a custom (remote) MCP connector and enter the URL above.",
                "If asked: Authentication = Sign in now, OAuth client = Register automatically.",
                "Sign in with your username and password.",
                "First time only: paste your YouTube headers when asked.",
            ),
            start=1,
        ):
            self.ui.note(f"{number}. {text}")
        self.ui.line()


def run_setup(
    options: Options,
    console: Console | None = None,
    runner: Runner | None = None,
    services: Services | None = None,
) -> int:
    console = console or Console()
    runner = runner or Runner(console, dry_run=options.dry_run)
    return Wizard(options, console, runner, services).execute()
