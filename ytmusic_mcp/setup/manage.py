"""Day-to-day commands for a server that the setup wizard installed.

    status    is everything healthy? (offline, safe to run any time or from cron)
    restart   restart the service and check it answers
    update    the quick path: pull the latest code, update Python packages, restart
    rebuild   the full path: packages, the Vite user-interface apps, the service unit,
              the web server config, then restart

`update` never touches the web server or builds anything heavy. `rebuild` re-applies
the whole installation from the code that is already checked out (it does not pull),
and it is the one to run when the UI source or the deployment files changed.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import PROJECT_DIR, ConfigError
from . import proxy, service
from .envfile import database_path, local_port, public_domain, read_env
from .proxy import CADDYFILE, StepFailed
from .system import Runner, memory_mb, read_text
from .ui import Console
from .wizard import PROXY_NAMES, Services, SetupError

WIDGETS_DIR = "widgets"
UI_APPS = ("playlist-manager", "oauth-login")  # the Vite apps under widgets/
INCOMING_SHOWN = 8


@dataclass
class ManageOptions:
    project_dir: Path = PROJECT_DIR
    dry_run: bool = False
    no_restart: bool = False  # update: pull and install, but leave the service running as it is
    skip_ui: bool = False  # rebuild: do not build the Vite apps
    build_all_ui: bool = False  # rebuild: also rebuild apps whose build output is committed to git
    proxy: str = "auto"  # rebuild: auto | caddy | nginx | none
    caddyfile: str = CADDYFILE


def _is_build_output(path: str) -> bool:
    return path.startswith(f"{WIDGETS_DIR}/") and "/dist/" in path


class Manager:
    def __init__(
        self, options: ManageOptions, console: Console, runner: Runner, services: Services | None = None
    ) -> None:
        self.o = options
        self.ui = console
        self.run = runner
        self.run.dry_run = options.dry_run or runner.dry_run  # one switch for everything
        self.svc = services or Services()
        self.domain = ""
        self.port = 8787
        self.env: dict[str, str] = {}

    # -- plumbing ----------------------------------------------------------------------------------

    def _git(self, *args: str, read_only: bool = True):
        return self.run.run(["git", "-C", str(self.o.project_dir), *args], read_only=read_only)

    def _guard(self, title: str, body: Callable[[], int]) -> int:
        """Shared start-up checks, banner, and error handling for every command."""
        self.ui.header(f"YouTube Music MCP  ·  {title}")
        if self.o.dry_run:
            self.ui.banner("Dry run: nothing will be changed", good=False)
        try:
            if (system := self.svc.system()) != "Linux":
                raise SetupError(f"These commands are for Linux servers (this is {system}).")
            if not self.run.which("systemctl"):
                raise SetupError("systemd was not found. Restart the app the way you started it.")
            self.env = read_env(self.o.project_dir / ".env")
            domain = public_domain(self.env)
            if not domain:
                raise SetupError("This server is not set up yet. Run ./deploy/install.sh first.")
            self.domain, self.port = domain, local_port(self.env)
            return body()
        except (SetupError, StepFailed, ConfigError) as error:
            self.ui.line()
            self.ui.fail(str(error))
            return 1
        except (KeyboardInterrupt, EOFError):
            self.ui.line()
            self.ui.warn("Cancelled.")
            return 130

    def _verify(self, attempts: int = 3) -> bool:
        results = proxy.wait_for_public(
            self.ui, self.svc.check_public, self.svc.sleep, self.domain, attempts=attempts, delay=3.0
        )
        return bool(results) and all(result.ok for result in results)

    def _head(self) -> str:
        return self._git("rev-parse", "--short", "HEAD").stdout.strip()

    def _install_packages(self) -> None:
        """Install the project into the running virtualenv (pulls in new or changed dependencies)."""
        command = [sys.executable, "-m", "pip", "install", "--quiet", "-e", str(self.o.project_dir)]
        result = self.run.run(command, timeout=900)
        if not result.ok:
            raise StepFailed("Installing the Python packages failed:\n" + result.stderr.strip()[-400:])
        self.ui.ok("Python packages are up to date")

    # -- status ------------------------------------------------------------------------------------------

    def status(self) -> int:
        return self._guard("Status", self._status)

    def _status(self) -> int:
        healthy = True
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        subject = self._git("log", "-1", "--format=%s").stdout.strip()
        self.ui.line()
        self.ui.table(
            [
                ("Address", f"https://{self.domain}/ytmusic-mcp"),
                ("Version", f"{branch} @ {self._head()}  {subject}" if branch else "not a git checkout"),
                ("Database", str(database_path(self.o.project_dir, self.env))),
            ]
        )
        self.ui.line()

        state = self.run.run(["systemctl", "is-active", service.SERVICE_NAME], read_only=True).stdout.strip()
        (self.ui.ok if state == "active" else self.ui.fail)(
            f"Service {service.SERVICE_NAME}: {state or 'unknown'}"
        )
        healthy &= state == "active"

        local = self.svc.check_local(self.port)
        (self.ui.ok if local.ok else self.ui.fail)(f"{local.name} ({local.detail})")
        healthy &= local.ok

        kind = proxy.detect_proxy(proxy.listeners(self.run))
        (self.ui.ok if kind in ("caddy", "nginx") else self.ui.warn)(f"Web server: {PROXY_NAMES[kind]}")

        for result in self.svc.check_public(self.domain):
            (self.ui.ok if result.ok else self.ui.fail)(f"{result.name} ({result.detail})")
            healthy &= result.ok

        self.ui.banner("Everything looks healthy" if healthy else "Problems found", good=healthy)
        if not healthy:
            self.ui.note(f"Logs: {service.JOURNAL_HINT}")
        return 0 if healthy else 1

    # -- restart -----------------------------------------------------------------------------------------

    def restart(self) -> int:
        return self._guard("Restart", self._restart)

    def _restart(self) -> int:
        self.ui.line()
        service.restart(self.run, self.ui, self.svc.check_local, self.svc.sleep, self.port)
        healthy = True if self.o.dry_run else self._verify()
        self.ui.banner("Restarted" if healthy else "Restarted, with items to check", good=healthy)
        return 0 if healthy else 1

    # -- update (quick) ----------------------------------------------------------------------------------------

    def update(self) -> int:
        return self._guard("Update", self._update)

    def _update(self) -> int:
        self.ui.step(1, 4, "Getting the latest code")
        before = self._pull()
        if before is None:
            return 0  # already up to date
        old, changed = before

        self.ui.step(2, 4, "Updating Python packages")
        self._install_packages()

        self.ui.step(3, 4, "Restarting the service")
        if self.o.no_restart:
            self.ui.warn(f"Skipped. Restart when ready: sudo systemctl restart {service.SERVICE_NAME}")
        else:
            service.restart(self.run, self.ui, self.svc.check_local, self.svc.sleep, self.port)

        self.ui.step(4, 4, "Checking it from the outside")
        healthy = True if (self.o.dry_run or self.o.no_restart) else self._verify()
        self._print_follow_ups(changed)
        new = self._head() if not self.o.dry_run else "(dry run)"
        self.ui.banner(f"Updated {old} -> {new}" if healthy else "Updated, with items to check", good=healthy)
        return 0 if healthy else 1

    def _pull(self) -> tuple[str, list[str]] | None:
        """Fast-forward to the remote branch. Returns (old commit, changed files), or None if nothing new."""
        if not self._git("rev-parse", "--is-inside-work-tree").ok:
            raise StepFailed(
                "This folder is not a git checkout, so it cannot update itself (install with git clone)."
            )
        old = self._head()
        self._discard_build_output_changes()
        upstream = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if not upstream.ok:
            raise StepFailed(
                "This branch does not follow a remote branch. Run: git branch --set-upstream-to=origin/<branch>"
            )
        remote = upstream.stdout.strip()

        if self.o.dry_run:
            self._git("fetch", "--quiet", read_only=False)
            self.ui.info(f"Would fast-forward {old} to {remote}")
            self._git("pull", "--ff-only", "--quiet", read_only=False)
            return old, []

        if not self._git("fetch", "--quiet", read_only=False).ok:
            raise StepFailed("Could not reach the remote repository. Check the network and your git access.")
        ahead, behind = (
            int(n) for n in self._git("rev-list", "--left-right", "--count", "HEAD...@{u}").stdout.split()
        )
        if ahead:
            raise StepFailed(
                f"This checkout and {remote} have diverged (the remote history may have been rewritten). "
                f"Nothing was changed. If you made no local commits, back up .env and data/, then run: "
                f"git reset --hard {remote}"
            )
        if not behind:
            self.ui.ok(f"Already up to date at {old}")
            self.ui.note("To re-apply the installation anyway: ./deploy/install.sh rebuild")
            return None
        self.ui.info(f"{behind} new commit{'s' if behind != 1 else ''}:")
        for line in self._git("log", "--format=%h %s", "HEAD..@{u}").stdout.splitlines()[:INCOMING_SHOWN]:
            self.ui.note(line)
        pulled = self._git("pull", "--ff-only", "--quiet", read_only=False)
        if not pulled.ok:
            raise StepFailed("git pull could not fast-forward:\n" + pulled.stderr.strip()[-400:])
        self.ui.ok(f"Now at {self._head()}")
        changed = self._git("diff", "--name-only", old, "HEAD").stdout.split()
        return old, changed

    def _discard_build_output_changes(self) -> None:
        """Locally rebuilt UI files are build output: restore them so they never block a pull."""
        lines = self._git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()
        paths = [line[3:].strip() for line in lines if line.strip()]
        other = [path for path in paths if not _is_build_output(path)]
        if other:
            shown = ", ".join(other[:5]) + (" ..." if len(other) > 5 else "")
            raise StepFailed(
                f"This checkout has local changes ({shown}). Commit, stash or discard them, then update again."
            )
        if paths:
            self.ui.info("Restoring locally rebuilt UI files to the committed versions")
            self._git("checkout", "--", *paths, read_only=False)

    def _print_follow_ups(self, changed: list[str]) -> None:
        if any(path.startswith(f"{WIDGETS_DIR}/") and "/src/" in path for path in changed):
            self.ui.warn("The UI source changed. Run ./deploy/install.sh rebuild to rebuild the UI apps.")
        tool_or_ui_files = (
            "ytmusic_mcp/tools.py",
            "ytmusic_mcp/ui.py",
            "ytmusic_mcp/youtube_ops.py",
            f"{WIDGETS_DIR}/playlist-manager/dist/",
        )
        if any(path.startswith(tool_or_ui_files) for path in changed):
            self.ui.warn("Tools or the playlist UI changed: refresh or re-add the connector in your client.")
        if any(path.startswith("deploy/") for path in changed):
            self.ui.warn("Deployment files changed. Run ./deploy/install.sh rebuild to apply them.")

    # -- rebuild (full) -----------------------------------------------------------------------------------------------

    def rebuild(self) -> int:
        return self._guard("Rebuild", self._rebuild)

    def _rebuild(self) -> int:
        self.ui.step(1, 5, "Python packages")
        self._install_packages()

        self.ui.step(2, 5, "User-interface apps (Vite)")
        self._build_ui()

        self.ui.step(3, 5, "Service unit")
        service.install_unit(self.run, self.o.project_dir, database_path(self.o.project_dir, self.env).parent)
        self.ui.ok("systemd unit re-applied")

        self.ui.step(4, 5, "Web server")
        self._reapply_web_server()

        self.ui.step(5, 5, "Restart and check")
        service.restart(self.run, self.ui, self.svc.check_local, self.svc.sleep, self.port)
        healthy = True if self.o.dry_run else self._verify()
        self.ui.banner("Rebuilt" if healthy else "Rebuilt, with items to check", good=healthy)
        return 0 if healthy else 1

    def _build_ui(self) -> None:
        """Build the Vite apps whose output is not committed (and the committed ones with --build-all-ui)."""
        if self.o.skip_ui:
            self.ui.warn("Skipped (--skip-ui).")
            return
        apps = [
            name for name in UI_APPS if (self.o.project_dir / WIDGETS_DIR / name / "package.json").exists()
        ]
        if not apps:
            self.ui.info("No UI apps found.")
            return
        if not self.run.which("npm"):
            self.ui.warn("Node.js (npm) is not installed, so the UI apps were not rebuilt.")
            self.ui.note(
                "The committed playlist UI and the plain sign-in page keep working. To build: install Node.js 18+."
            )
            return
        total, swap = memory_mb(read_text("/proc/meminfo"))
        if 0 < total < 1500 and swap == 0:
            self.ui.warn(f"{total} MB of memory and no swap: building can run out of memory.")
            self.ui.note("Add a swap file first, or build on another computer and copy dist/index.html over.")
        built = 0
        for name in apps:
            app_dir = f"{WIDGETS_DIR}/{name}"
            committed = self._git("ls-files", "--error-unmatch", f"{app_dir}/dist/index.html").ok
            if committed and not self.o.build_all_ui:
                self.ui.info(f"{name}: the build is committed to git, so that one is used as is")
                self.ui.note("Rebuild it on your computer and commit it, or use --build-all-ui here.")
                continue
            prefix = str(self.o.project_dir / app_dir)
            if not self.run.run(
                ["npm", "--prefix", prefix, "ci", "--no-audit", "--no-fund", "--loglevel=error"], timeout=900
            ).ok:
                raise StepFailed(f"npm could not install the packages for {name}.")
            if not self.run.run(["npm", "--prefix", prefix, "run", "build"], timeout=900).ok:
                raise StepFailed(
                    f"The build of {name} failed. Run it by hand to see the full error: npm --prefix {prefix} run build"
                )
            self.ui.ok(f"Built {name}")
            built += 1
        if built == 0:
            self.ui.ok("Nothing needed building")

    def _reapply_web_server(self) -> None:
        kind = self.o.proxy
        if kind == "auto":
            kind = proxy.detect_proxy(proxy.listeners(self.run))
        if kind == "caddy":
            # replace_existing=False: our own block is updated automatically, someone else's site is never replaced.
            backup = proxy.configure_caddy(
                self.run, self.ui, self.domain, self.port, replace_existing=False, caddyfile=self.o.caddyfile
            )
            if backup:
                self.ui.note(f"Backup: {backup}")
        elif kind == "nginx":
            proxy.configure_nginx(self.run, self.ui, self.domain, self.port)
        else:
            self.ui.info(f"Skipped: {PROXY_NAMES.get(kind, kind)} is not managed by this tool.")


def run_manage(
    command: str,
    options: ManageOptions,
    console: Console | None = None,
    runner: Runner | None = None,
    services: Services | None = None,
) -> int:
    console = console or Console()
    runner = runner or Runner(console, dry_run=options.dry_run)
    manager = Manager(options, console, runner, services)
    return {
        "status": manager.status,
        "restart": manager.restart,
        "update": manager.update,
        "rebuild": manager.rebuild,
    }[command]()
