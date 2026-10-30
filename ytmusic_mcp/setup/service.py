"""The systemd service that runs the app: install its unit, restart it, wait until it answers."""

import getpass
from collections.abc import Callable
from pathlib import Path

from .proxy import DEPLOY_DIR, Check, StepFailed
from .system import Runner
from .ui import Console

SERVICE_NAME = "ytmusic-mcp"
UNIT_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
JOURNAL_HINT = f"journalctl -u {SERVICE_NAME} -n 40 --no-pager"


def unit_text(project_dir: Path, data_dir: Path) -> str:
    """The unit file for this machine, filled in from deploy/ytmusic-mcp.service.template."""
    text = (DEPLOY_DIR / "ytmusic-mcp.service.template").read_text(encoding="utf-8")
    for placeholder, value in (
        ("@USER@", getpass.getuser()),
        ("@APP_DIR@", str(project_dir)),
        ("@DATA_DIR@", str(data_dir)),
    ):
        text = text.replace(placeholder, value)
    return text


def install_unit(runner: Runner, project_dir: Path, data_dir: Path) -> None:
    """Write the unit file and enable it (does not restart)."""
    if not runner.install_file(unit_text(project_dir, data_dir), UNIT_PATH).ok:
        raise StepFailed("Could not install the systemd unit.")
    for command in (["systemctl", "daemon-reload"], ["systemctl", "enable", SERVICE_NAME]):
        if not runner.run(command, sudo=True).ok:
            raise StepFailed(f"'{' '.join(command)}' failed. See: {JOURNAL_HINT}")


def restart(
    runner: Runner,
    console: Console,
    check_local: Callable[[int], Check],
    sleep: Callable[[float], None],
    port: int,
) -> None:
    """Restart the service and wait (up to ~20 s) until the app answers on this machine."""
    if not runner.run(["systemctl", "restart", SERVICE_NAME], sudo=True).ok:
        raise StepFailed(f"'systemctl restart {SERVICE_NAME}' failed. See: {JOURNAL_HINT}")
    if runner.dry_run:
        return
    for _ in range(10):  # the first start can take a few seconds
        check = check_local(port)
        if check.ok:
            console.ok(f"The app is running on this machine ({check.detail})")
            return
        sleep(2)
    raise StepFailed(f"The service did not start. See: {JOURNAL_HINT}")
