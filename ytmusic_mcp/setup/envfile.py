"""Reading and updating the .env file without disturbing the rest of it."""

import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

ASSIGNMENT = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$")


def read_env(path: Path) -> dict[str, str]:
    """The active (uncommented) KEY=value pairs of an env file."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return values


def set_env(path: Path, updates: dict[str, str], template: Path | None = None) -> None:
    """Set keys in place, keeping comments and other lines. Missing keys are appended.

    A commented-out example of a key (``# KEY=...``) stays as it is. If the file
    does not exist it is started from `template` (the project's .env.example).
    """
    if not path.exists():
        if template is not None and template.exists():
            shutil.copyfile(template, path)
        else:
            path.write_text("", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    for index, line in enumerate(lines):
        match = ASSIGNMENT.match(line)
        if match and match.group(1) in remaining:
            lines[index] = f"{match.group(1)}={remaining.pop(match.group(1))}"
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def public_domain(env: dict[str, str]) -> str | None:
    """The host name from MCP_PUBLIC_URL, or None when it is missing or still the template value."""
    url = env.get("MCP_PUBLIC_URL", "")
    if not url or "yourdomain" in url:
        return None
    return urlparse(url).netloc or None


def local_port(env: dict[str, str]) -> int:
    try:
        return int(env.get("MCP_PORT", "8787"))
    except ValueError:
        return 8787


def database_path(project_dir: Path, env: dict[str, str]) -> Path:
    """Where the SQLite database lives for this project's settings."""
    configured = env.get("YTMUSIC_DB_PATH") or env.get("OAUTH_DB_PATH")
    return Path(configured).expanduser() if configured else project_dir / "data" / "ytmusic-mcp.db"
