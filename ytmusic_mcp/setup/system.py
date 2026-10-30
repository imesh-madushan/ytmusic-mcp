"""Running commands and looking at the machine, for the setup wizard.

Parsing is kept in small pure functions so it can be tested without a server.
Only `Runner` touches the system, and it can run in dry-run mode.
"""

import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .ui import Console

NETWORK_FILESYSTEMS = {
    "nfs",
    "nfs4",
    "cifs",
    "smb",
    "smbfs",
    "smb3",
    "fuse",
    "fuse.sshfs",
    "9p",
    "afs",
    "ceph",
}


@dataclass
class Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner:
    """Runs commands. Changes are shown first; in dry-run mode they are shown but not executed."""

    def __init__(self, console: Console, dry_run: bool = False):
        self.console = console
        self.dry_run = dry_run

    def run(
        self,
        argv: list[str],
        *,
        sudo: bool = False,
        read_only: bool = False,
        interactive: bool = False,
        timeout: int = 120,
    ) -> Result:
        """Run a command.

        `read_only` probes always run, even in dry-run, and are not echoed.
        `interactive` hands the terminal to the command (for tools that ask questions).
        """
        if sudo and hasattr(os, "geteuid") and os.geteuid() != 0:
            argv = ["sudo", *argv]
        if not read_only:
            self.console.command(" ".join(argv))
            if self.dry_run:
                return Result()
        try:
            done = subprocess.run(
                argv, capture_output=not interactive, text=True, timeout=timeout, check=False
            )
        except FileNotFoundError:
            return Result(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"{argv[0]}: timed out after {timeout}s")
        return Result(done.returncode, done.stdout or "", done.stderr or "")

    def install_file(self, text: str, destination: str, mode: str = "0644") -> Result:
        """Put `text` at `destination` (root-owned when needed) via a temporary file."""
        if self.dry_run:
            self.console.command(f"install -m {mode} <generated file> {destination}")
            return Result()
        folder = Path(tempfile.mkdtemp(prefix="ytmusic-setup-"))
        source = folder / "file"
        source.write_text(text, encoding="utf-8")
        try:
            return self.run(["install", "-m", mode, str(source), destination], sudo=True)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def read_file(self, path: str) -> str | None:
        """Contents of a file, using sudo if needed. None when it does not exist or cannot be read."""
        try:
            return Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            result = self.run(["cat", path], sudo=True, read_only=True)
            return result.stdout if result.ok else None

    @staticmethod
    def which(name: str) -> str | None:
        return shutil.which(name)


# -- pure parsers ------------------------------------------------------------------------


def parse_listeners(ss_output: str) -> dict[int, str]:
    """Map listening TCP port -> process name from `ss -tlnp` output ('' when not visible)."""
    listeners: dict[int, str] = {}
    for line in ss_output.splitlines():
        parts = line.split()
        if not parts or parts[0] != "LISTEN" or len(parts) < 5:
            continue
        port_match = re.search(r":(\d+)$", parts[3])
        if not port_match:
            continue
        process = re.search(r'users:\(\("([^"]+)"', line)
        listeners[int(port_match.group(1))] = (
            process.group(1) if process else listeners.get(int(port_match.group(1)), "")
        )
    return listeners


def filesystem_of(path: Path, mounts_text: str) -> str:
    """Filesystem type of the mount that contains `path` (longest matching mount point)."""
    best_mount, best_type = "", ""
    target = str(path).replace("\\", "/")
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point = fields[1].replace("\\040", " ").replace("\\", "/")
        if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) and len(
            mount_point
        ) > len(best_mount):
            best_mount, best_type = mount_point, fields[2]
    return best_type


def memory_mb(meminfo_text: str) -> tuple[int, int]:
    """(total RAM, total swap) in MB from /proc/meminfo."""

    def kilobytes(key: str) -> int:
        found = re.search(rf"^{key}:\s+(\d+)\s*kB", meminfo_text, re.MULTILINE)
        return int(found.group(1)) if found else 0

    return kilobytes("MemTotal") // 1024, kilobytes("SwapTotal") // 1024


# -- probes that look at the machine ----------------------------------------------------------


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def resolve_ipv4(domain: str) -> list[str]:
    try:
        return sorted(set(socket.gethostbyname_ex(domain)[2]))
    except OSError:
        return []


def public_ip(timeout: float = 4.0) -> str | None:
    """This machine's public IPv4 address, or None when it cannot be found."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as response:
            text = response.read().decode().strip()
        return text if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", text) else None
    except OSError:
        return None
