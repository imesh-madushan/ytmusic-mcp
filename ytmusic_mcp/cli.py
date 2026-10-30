"""Admin command line: manage users and their YouTube cookies.

python -m ytmusic_mcp.cli genkey
python -m ytmusic_mcp.cli add-user alice
python -m ytmusic_mcp.cli set-cookies alice [--file headers.txt]
python -m ytmusic_mcp.cli check-cookies alice
python -m ytmusic_mcp.cli local-cookies [--file headers.txt]   (stdio mode)
"""

import argparse
import getpass
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from ytmusicapi import YTMusic

from .config import ConfigError, Settings, load_settings
from .errors import AccountError
from .headers import build_auth_headers, encrypt_headers, headers_from_text
from .security import CookieVault, generate_secret_key, hash_password
from .store import Store

HEADER_STEPS = """
Steps:
  1. Open music.youtube.com and make sure you are logged in.
  2. Open DevTools (F12) -> Network, then reload the page.
  3. Select a music.youtube.com/youtubei/v1/browse request.
  4. Copy all request headers.
"""


def read_headers_text(file: str | None) -> str:
    """Read headers from a file, or from a paste ending in an empty line."""
    if file:
        return Path(file).read_text(encoding="utf-8")
    print(HEADER_STEPS)
    if sys.stdin.isatty():
        print("Paste the headers, then press Enter on an empty line:")
    lines: list[str] = []
    for line in sys.stdin:
        if not line.strip():
            break
        lines.append(line.rstrip("\n"))
    return "\n".join(lines)


def prompt_password() -> str:
    while True:
        password = getpass.getpass("Password: ")
        if len(password) < 12:
            print("Use at least 12 characters.")
        elif password != getpass.getpass("Repeat password: "):
            print("The passwords do not match.")
        else:
            return password


def open_store(settings: Settings) -> tuple[Store, CookieVault]:
    return Store(settings.db_path), CookieVault(settings.require_secret_key())


def require_user(store: Store, username: str) -> None:
    if store.get_user(username) is None:
        raise AccountError(f"No user named {username!r}.")


# -- commands ----------------------------------------------------------------------------


def cmd_genkey(args: argparse.Namespace, settings: Settings) -> None:
    print(generate_secret_key())
    print(
        "Put this in .env as YTMUSIC_SECRET_KEY. Back it up: without it, stored cookies are unreadable.",
        file=sys.stderr,
    )


def cmd_add_user(args: argparse.Namespace, settings: Settings) -> None:
    store, _ = open_store(settings)
    try:
        store.create_user(args.username, hash_password(prompt_password()))
    except sqlite3.IntegrityError:
        raise AccountError(f"User {args.username!r} already exists.") from None
    print(f"Created {args.username}. Next: set-cookies {args.username}")


def cmd_set_password(args: argparse.Namespace, settings: Settings) -> None:
    store, _ = open_store(settings)
    require_user(store, args.username)
    store.set_password(args.username, hash_password(prompt_password()))
    print(f"Password updated for {args.username}.")


def cmd_set_cookies(args: argparse.Namespace, settings: Settings) -> None:
    store, vault = open_store(settings)
    require_user(store, args.username)
    headers = headers_from_text(read_headers_text(args.file))
    store.set_cookies(args.username, encrypt_headers(headers, vault))
    print(f"Encrypted cookies saved for {args.username}.")


def cmd_check_cookies(args: argparse.Namespace, settings: Settings) -> None:
    store, vault = open_store(settings)
    user = store.get_user(args.username)
    if user is None or user.cookies is None:
        raise AccountError(f"{args.username!r} has no stored cookies.")
    client = YTMusic(build_auth_headers(json.loads(vault.decrypt(user.cookies))))
    playlists = client.get_library_playlists(limit=1)
    print(f"Cookies work (the library returned {len(playlists)} playlist(s)).")


def cmd_list_users(args: argparse.Namespace, settings: Settings) -> None:
    store, _ = open_store(settings)
    for user in store.list_users():
        updated = datetime.fromtimestamp(user.updated_at).strftime("%Y-%m-%d %H:%M")
        state = f"cookies updated {updated}" if user.cookies else "no cookies yet"
        print(f"{user.username}\t{state}")


def cmd_delete_user(args: argparse.Namespace, settings: Settings) -> None:
    store, _ = open_store(settings)
    if not store.delete_user(args.username):
        raise AccountError(f"No user named {args.username!r}.")
    print(f"Deleted {args.username} and revoked their tokens.")


def cmd_local_cookies(args: argparse.Namespace, settings: Settings) -> None:
    """Write the cookie file used by stdio mode."""
    headers = headers_from_text(read_headers_text(args.file))
    build_auth_headers(headers)
    path = settings.local_auth_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(headers, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"Saved cookies to {path}")


def cmd_setup(args: argparse.Namespace, settings: Settings) -> None:
    """Guided install: configuration, user, web server, service."""
    from .setup.wizard import Options, run_setup

    code = run_setup(
        Options(
            domain=args.domain,
            username=args.username,
            proxy=args.proxy,
            port=args.port,
            yes=args.yes,
            dry_run=args.dry_run,
            replace_site=True if args.replace_site else None,
            skip_service=args.skip_service,
            caddyfile=args.caddyfile,
        )
    )
    if code:
        raise SystemExit(code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ytmusic_mcp.cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("genkey", help="print a new YTMUSIC_SECRET_KEY").set_defaults(run=cmd_genkey)
    commands.add_parser("list-users", help="list users").set_defaults(run=cmd_list_users)

    for name, run, help_text in (
        ("add-user", cmd_add_user, "create a user"),
        ("set-password", cmd_set_password, "change a user's password"),
        ("set-cookies", cmd_set_cookies, "store a user's YouTube cookies (encrypted)"),
        ("check-cookies", cmd_check_cookies, "test that a user's cookies still work"),
        ("delete-user", cmd_delete_user, "delete a user and revoke their tokens"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("username")
        if name == "set-cookies":
            sub.add_argument("--file", help="read headers from this file instead of pasting")
        sub.set_defaults(run=run)

    local = commands.add_parser("local-cookies", help="save cookies for stdio (single-user) mode")
    local.add_argument("--file", help="read headers from this file instead of pasting")
    local.set_defaults(run=cmd_local_cookies)

    setup = commands.add_parser("setup", help="guided server install (config, user, web server, service)")
    setup.add_argument("--domain", help="public host name, e.g. mcp.example.com")
    setup.add_argument("--username", help="name of the first sign-in user")
    setup.add_argument(
        "--proxy", choices=["auto", "caddy", "nginx", "none"], default="auto", help="web server to configure"
    )
    setup.add_argument("--port", type=int, help="local port for the app (default 8787)")
    setup.add_argument(
        "--yes",
        action="store_true",
        help="no questions; needs --domain, --username and YTMUSIC_SETUP_PASSWORD",
    )
    setup.add_argument(
        "--dry-run", action="store_true", help="show what would happen without changing anything"
    )
    setup.add_argument(
        "--replace-site", action="store_true", help="allow replacing an existing Caddy site for the domain"
    )
    setup.add_argument(
        "--caddyfile",
        default="/etc/caddy/Caddyfile",
        help="path of the Caddyfile to edit (default /etc/caddy/Caddyfile)",
    )
    setup.add_argument("--skip-service", action="store_true", help="do not install the systemd service")
    setup.set_defaults(run=cmd_setup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.run(args, load_settings())
    except (AccountError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
