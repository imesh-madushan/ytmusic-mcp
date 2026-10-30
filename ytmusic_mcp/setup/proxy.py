"""Putting a web server (Caddy or nginx) in front of the app, safely.

The rule for anything that edits a web server which may already host other
sites: back it up, validate the change before applying it, apply it gracefully,
check that nothing else stopped working, and restore the backup if it did.
"""

import re
import secrets
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..config import PROJECT_DIR
from .system import Runner, parse_listeners
from .ui import Console

DEPLOY_DIR = PROJECT_DIR / "deploy"
CADDYFILE = "/etc/caddy/Caddyfile"
NGINX_SITE = "/etc/nginx/sites-available/ytmusic-mcp"
NGINX_LINK = "/etc/nginx/sites-enabled/ytmusic-mcp"
# Paths a front web server must forward to the app, in addition to /ytmusic-mcp (which maps to /mcp).
OAUTH_PATHS = [
    "/register",
    "/authorize",
    "/token",
    "/revoke",
    "/login",
    "/login/callback",
    "/youtube-auth",
    "/youtube-auth/callback",
    "/.well-known/oauth-protected-resource/ytmusic-mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/ytmusic-mcp",
    "/.well-known/openid-configuration",
]
SECRET_PATTERNS = [re.compile(r"\$2[aby]\$\d+\$[A-Za-z0-9./]+")]  # bcrypt hashes in basic-auth blocks


class StepFailed(Exception):
    """A setup step could not be completed (and was undone where needed)."""


# -- templates and Caddyfile parsing (pure) -----------------------------------------------------


def render(template: str, **values: str) -> str:
    """Fill `${NAME}` placeholders. Other `$` text (nginx variables) is left alone."""
    text = (DEPLOY_DIR / template).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("${" + key + "}", value)
    return text


def caddy_site_block(domain: str, port: int) -> str:
    return render("caddy-site.template", MCP_DOMAIN=domain, MCP_PORT=str(port))


def mask_secrets(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("<hash hidden>", text)
    return text


@dataclass(frozen=True)
class Block:
    addresses: list[str]
    start: int  # first line index
    end: int  # last line index (the closing brace)


def top_level_blocks(text: str) -> list[Block]:
    """The top-level `address { ... }` blocks of a Caddyfile, found by counting braces."""
    blocks: list[Block] = []
    depth, start, addresses = 0, 0, []
    for index, raw in enumerate(text.splitlines()):
        code = raw.split("#", 1)[0].rstrip()
        if depth == 0 and code.endswith("{"):
            # A lone "{" is Caddy's global options block, not a site.
            start, addresses = index, [a.rstrip(",") for a in code[:-1].split()]
        before = depth
        depth += code.count("{") - code.count("}")
        if before > 0 and depth == 0 and addresses:
            blocks.append(Block(addresses, start, index))
            addresses = []
    return blocks


def _host(address: str) -> str:
    return re.sub(r"^https?://", "", address).split("/", 1)[0].lower()


def find_site(text: str, domain: str) -> Block | None:
    for block in top_level_blocks(text):
        if any(_host(address) == domain.lower() for address in block.addresses):
            return block
    return None


def with_site(text: str, domain: str, block_text: str, replace: bool) -> str:
    """Add the site block at the end, or replace the existing block for `domain`."""
    existing = find_site(text, domain)
    if existing and replace:
        lines = text.splitlines()
        lines[existing.start : existing.end + 1] = block_text.rstrip("\n").splitlines()
        return "\n".join(lines) + "\n"
    return text.rstrip("\n") + "\n\n" + block_text.rstrip("\n") + "\n"


# -- Caddy ----------------------------------------------------------------------------------------


def _listening_ports(runner: Runner) -> set[int]:
    return set(parse_listeners(runner.run(["ss", "-tln"], read_only=True).stdout))


def configure_caddy(
    runner: Runner,
    console: Console,
    domain: str,
    port: int,
    *,
    replace_existing: bool | None,
    caddyfile: str = CADDYFILE,
) -> str:
    """Add (or, with consent, replace) the site for `domain`. Returns the backup path."""
    original = runner.read_file(caddyfile)
    if original is None:
        raise StepFailed(f"Cannot read {caddyfile}. Is Caddy installed?")
    existing = find_site(original, domain)
    replace = False
    if existing:
        console.warn(f"Caddy already serves {domain}:")
        for line in mask_secrets(
            "\n".join(original.splitlines()[existing.start : existing.end + 1])
        ).splitlines()[:12]:
            console.note(line)
        replace = (
            replace_existing
            if replace_existing is not None
            else console.confirm("Replace it? (a backup of the current file is kept)", default=False)
        )
        if not replace:
            raise StepFailed(
                f"{domain} is already used by another Caddy site; choose another domain or allow replacing it."
            )

    new_text = with_site(original, domain, caddy_site_block(domain, port), replace)
    with tempfile.TemporaryDirectory(prefix="ytmusic-caddy-") as folder:
        candidate = Path(folder) / "Caddyfile"
        candidate.write_text(new_text, encoding="utf-8")
        if runner.which("caddy"):
            checked = runner.run(
                ["caddy", "validate", "--config", str(candidate), "--adapter", "caddyfile"], read_only=True
            )
            if not checked.ok:
                raise StepFailed(
                    "Caddy rejected the new configuration, so nothing was changed.\n"
                    + checked.stderr.strip()[-400:]
                )
            console.ok("Caddy accepts the new configuration")

    backup = f"{caddyfile}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    if not runner.run(["cp", "-p", caddyfile, backup], sudo=True).ok:
        raise StepFailed("Could not back up the Caddyfile, so nothing was changed.")
    console.ok(f"Backed up the current config to {backup}")

    ports_before = _listening_ports(runner)

    def undo(reason: str) -> StepFailed:
        runner.install_file(original, caddyfile)
        runner.run(["systemctl", "reload", "caddy"], sudo=True)
        return StepFailed(f"{reason} The previous Caddy config was restored.")

    if (
        not runner.install_file(new_text, caddyfile).ok
        or not runner.run(["systemctl", "reload", "caddy"], sudo=True).ok
    ):
        raise undo("Caddy could not load the new configuration.")
    if not runner.dry_run:
        time.sleep(3)  # let Caddy finish applying the reload
        if runner.run(["systemctl", "is-active", "caddy"], read_only=True).stdout.strip() != "active":
            raise undo("Caddy is not running after the reload.")
        if not ports_before <= _listening_ports(runner):
            raise undo("Some ports that were open before the change are no longer listening.")
    console.ok("Caddy reloaded; your other sites are still listening")
    return backup


# -- nginx ------------------------------------------------------------------------------------------


def configure_nginx(runner: Runner, console: Console, domain: str, port: int) -> str:
    """Get a certificate first (the config refers to it), then install the nginx site."""
    certificate = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
    if runner.run(["test", "-f", certificate], sudo=True, read_only=True).ok:
        console.ok(f"Certificate for {domain} already exists")
    else:
        console.info("Getting a certificate from Let's Encrypt (certbot may ask for an email address):")
        result = runner.run(
            ["certbot", "certonly", "--webroot", "-w", "/var/www/html", "-d", domain],
            sudo=True,
            interactive=True,
            timeout=600,
        )
        if not result.ok:
            raise StepFailed(
                "certbot could not get a certificate. Check that DNS points here and port 80 is reachable."
            )

    previous = runner.read_file(NGINX_SITE)
    config = render("nginx.conf.template", MCP_DOMAIN=domain, MCP_PORT=str(port))

    def undo(reason: str) -> StepFailed:
        if previous is None:
            runner.run(["rm", "-f", NGINX_LINK, NGINX_SITE], sudo=True)
        else:
            runner.install_file(previous, NGINX_SITE)
        return StepFailed(f"{reason} The previous nginx config was restored.")

    if not runner.install_file(config, NGINX_SITE).ok:
        raise undo("Could not write the nginx config.")
    runner.run(["ln", "-sf", NGINX_SITE, NGINX_LINK], sudo=True)
    if not runner.run(["nginx", "-t"], sudo=True).ok:
        raise undo("nginx rejected the configuration.")
    if not runner.run(["systemctl", "reload", "nginx"], sudo=True).ok:
        raise undo("nginx could not reload.")
    console.ok("nginx is serving the app")
    return NGINX_SITE


# -- checks against the running site --------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def fetch(url: str, timeout: float = 15.0) -> tuple[int, dict[str, str]]:
    """GET a URL and return (status, lower-cased headers). HTTP errors are results, not exceptions."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}
    except OSError as error:
        return 0, {"error": str(error)}


def check_local(port: int) -> Check:
    status, _ = fetch(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server", timeout=5)
    return Check("App answers on this machine", status == 200, f"HTTP {status}" if status else "no response")


def check_public(domain: str) -> list[Check]:
    base = f"https://{domain}"
    status, headers = fetch(f"{base}/ytmusic-mcp")
    asks_for_token = status == 401 and "bearer" in headers.get("www-authenticate", "").lower()
    unknown, _ = fetch(f"{base}/{secrets.token_hex(4)}")
    metadata, _ = fetch(f"{base}/.well-known/oauth-authorization-server")
    return [
        Check(
            "MCP endpoint asks for an OAuth token",
            asks_for_token,
            f"HTTP {status}" if status else headers.get("error", "no response"),
        ),
        Check("Other paths are not exposed", unknown == 404, f"HTTP {unknown}"),
        Check("OAuth metadata is published", metadata == 200, f"HTTP {metadata}"),
    ]
