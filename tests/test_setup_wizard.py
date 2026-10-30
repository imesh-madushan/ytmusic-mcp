"""The setup wizard end to end, against a recording runner and canned services."""

import getpass

import pytest
from setup_fakes import (
    CADDYFILE,
    PLAIN_SS_BEFORE,
    SS_CADDY,
    SS_EMPTY,
    FakeRunner,
    healthy_services,
    make_console,
    make_options,
)

from ytmusic_mcp.security import generate_secret_key, verify_password
from ytmusic_mcp.setup import proxy, wizard
from ytmusic_mcp.setup.envfile import read_env
from ytmusic_mcp.setup.proxy import Check
from ytmusic_mcp.setup.system import Result
from ytmusic_mcp.setup.wizard import (
    Wizard,
    detect_proxy,
    validate_domain,
    validate_password,
    validate_username,
)
from ytmusic_mcp.store import Store

CADDY_PATH = "/etc/caddy/Caddyfile"
UNIT_PATH = "/etc/systemd/system/ytmusic-mcp.service"
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(proxy.time, "sleep", lambda seconds: None)


def caddy_machine(console, **kwargs):
    responses = {("ss", "-tlnp"): Result(stdout=SS_CADDY), ("ss", "-tln"): Result(stdout=PLAIN_SS_BEFORE)}
    responses.update(kwargs.pop("responses", {}))
    return FakeRunner(console, files={CADDY_PATH: CADDYFILE}, responses=responses, **kwargs)


def interactive_run(tmp_path, answers=None, secrets=None, runner=None, services=None, **option_overrides):
    """Run the full wizard with scripted answers; returns (exit code, output, runner, project)."""
    answers = ["mcp.example.com", "alice", "", ""] if answers is None else answers
    secrets = [PASSWORD, PASSWORD] if secrets is None else secrets
    console, out = make_console(answers, secrets)
    runner = runner or caddy_machine(console)
    runner.console = console
    options = make_options(tmp_path, **option_overrides)
    code = Wizard(options, console, runner, services or healthy_services()).execute()
    return code, out.getvalue(), runner, tmp_path


# -- the happy path --------------------------------------------------------------------------------


def test_full_setup_on_a_machine_running_caddy(tmp_path):
    code, output, runner, project = interactive_run(tmp_path)
    assert code == 0

    env = read_env(project / ".env")
    assert env["MCP_PUBLIC_URL"] == "https://mcp.example.com/ytmusic-mcp"
    assert env["MCP_HOST"] == "127.0.0.1" and env["MCP_TRANSPORT"] == "http"
    assert len(env["YTMUSIC_SECRET_KEY"]) == 44  # a generated Fernet key

    user = Store(project / "data" / "ytmusic-mcp.db").get_user("alice")
    assert user is not None and verify_password(PASSWORD, user.password_hash)
    assert PASSWORD not in (project / ".env").read_text(encoding="utf-8")

    unit = runner.installed[UNIT_PATH]
    assert f"User={getpass.getuser()}" in unit and str(project) in unit and "@" not in unit
    caddyfile = runner.installed[CADDY_PATH]
    assert "mcp.example.com {" in caddyfile and "http://100.64.0.10:8080 {" in caddyfile

    steps = [
        c for c in runner.calls if c[0] == "systemctl" and c[1] in ("daemon-reload", "enable", "restart")
    ]
    assert [c[1] for c in steps] == ["daemon-reload", "enable", "restart"]
    assert "https://mcp.example.com/ytmusic-mcp" in output and "All set" in output
    assert "Sign in now" in output  # connector instructions


def test_the_screen_shows_every_step_in_order(tmp_path):
    _, output, _, _ = interactive_run(tmp_path)
    positions = [output.index(f"Step {n} of 6") for n in range(1, 7)]
    assert positions == sorted(positions)


def test_headers_pasted_during_setup_are_stored_encrypted(tmp_path):
    cookie_text = "curl 'https://music.youtube.com/youtubei/v1/next' -H 'x-goog-authuser: 0' -H 'origin: https://music.youtube.com' -b '__Secure-3PAPISID=abc123; SID=x'"
    answers = [
        "mcp.example.com",
        "alice",
        "",
        "y",
        cookie_text,
        "",
    ]  # apply, paste? yes, headers, end of paste
    code, output, _, project = interactive_run(tmp_path, answers=answers)
    assert code == 0 and "YouTube headers saved" in output
    cookies = Store(project / "data" / "ytmusic-mcp.db").get_user("alice").cookies
    assert cookies is not None and b"abc123" not in cookies


def test_bad_pasted_headers_do_not_stop_the_setup(tmp_path):
    answers = ["mcp.example.com", "alice", "", "y", "not headers", ""]
    code, output, _, project = interactive_run(tmp_path, answers=answers)
    assert code == 0 and "could not be used" in output
    assert Store(project / "data" / "ytmusic-mcp.db").get_user("alice").cookies is None


# -- non-interactive and dry-run ------------------------------------------------------------------------


def test_yes_mode_runs_without_questions(tmp_path):
    services = healthy_services(password_from_env=lambda: PASSWORD)
    code, _, _, project = interactive_run(
        tmp_path,
        answers=[],
        secrets=[],
        services=services,
        yes=True,
        domain="mcp.example.com",
        username="alice",
    )
    assert code == 0 and Store(project / "data" / "ytmusic-mcp.db").get_user("alice") is not None


def test_yes_mode_needs_the_password_from_the_environment(tmp_path):
    code, output, runner, project = interactive_run(
        tmp_path, answers=[], secrets=[], yes=True, domain="mcp.example.com", username="alice"
    )
    assert code == 1 and "YTMUSIC_SETUP_PASSWORD" in output
    assert not (project / ".env").exists() and runner.installed == {}


def test_dry_run_changes_nothing(tmp_path):
    code, output, runner, project = interactive_run(
        tmp_path, answers=["mcp.example.com", "alice"], secrets=[], dry_run=True
    )
    assert code == 0 and "Dry run" in output
    assert not (project / ".env").exists() and not (project / "data").exists()
    assert runner.installed == {}


def test_cancelling_at_the_plan_changes_nothing(tmp_path):
    code, output, runner, project = interactive_run(tmp_path, answers=["mcp.example.com", "alice", "n"])
    assert code == 1 and "Cancelled" in output
    assert not (project / ".env").exists() and runner.installed == {}


# -- different machines ----------------------------------------------------------------------------------------


def test_no_web_server_offers_to_install_caddy(tmp_path):
    console, _ = make_console()
    runner = FakeRunner(
        console,
        files={CADDY_PATH: ":80 {\n\trespond hello\n}\n"},
        responses={("ss", "-tlnp"): Result(stdout=SS_EMPTY), ("ss", "-tln"): Result(stdout=PLAIN_SS_BEFORE)},
    )
    answers = ["mcp.example.com", "alice", "", "", ""]  # apply, paste? no, install Caddy? yes (default)
    code, _, runner, _ = interactive_run(tmp_path, answers=answers, runner=runner)
    assert code == 0 and runner.ran("apt-get", "install", "-y", "caddy")
    caddyfile = runner.installed[CADDY_PATH]
    assert "mcp.example.com {" in caddyfile and ":80 {" in caddyfile


def test_some_other_web_server_gets_instructions_instead_of_changes(tmp_path):
    console, _ = make_console()
    ss = 'LISTEN 0 511 *:443 *:* users:(("apache2",pid=9,fd=4))\nLISTEN 0 511 *:80 *:* users:(("apache2",pid=9,fd=3))\n'
    runner = FakeRunner(console, responses={("ss", "-tlnp"): Result(stdout=ss)})
    code, output, runner, _ = interactive_run(tmp_path, runner=runner)
    assert code == 0 and CADDY_PATH not in runner.installed
    assert (
        "set up by hand" in output and "127.0.0.1:8787/mcp" in output and "/youtube-auth/callback" in output
    )


def test_existing_caddy_site_for_the_domain_stops_setup_cleanly(tmp_path):
    answers = ["other.example.com", "alice", "", "", "n"]  # apply, paste? no, replace the site? no
    code, output, runner, _ = interactive_run(tmp_path, answers=answers)
    assert code == 1 and "already used by another Caddy site" in output
    assert CADDY_PATH not in runner.installed
    assert "<hash hidden>" in output


def test_existing_caddy_site_can_be_replaced_with_the_flag(tmp_path):
    answers = ["other.example.com", "alice", "", ""]
    code, _, runner, _ = interactive_run(tmp_path, answers=answers, replace_site=True)
    assert code == 0 and "fileuser" not in runner.installed[CADDY_PATH]


def test_failing_checks_are_reported_and_the_exit_code_is_nonzero(tmp_path):
    services = healthy_services(
        check_public=lambda domain: [
            Check("MCP endpoint asks for an OAuth token", False, "HTTP 401 from Basic auth")
        ]
    )
    code, output, _, _ = interactive_run(tmp_path, services=services)
    assert code == 1 and "items to check" in output and "Basic auth" in output


def test_a_service_that_never_starts_is_a_clear_failure(tmp_path):
    services = healthy_services(check_local=lambda port: Check("App", False, "no response"))
    code, output, _, _ = interactive_run(tmp_path, services=services)
    assert code == 1 and "did not start" in output and "journalctl" in output


def test_service_step_is_skipped_without_systemd(tmp_path):
    console, _ = make_console()
    runner = caddy_machine(console, tools=("caddy",))
    code, output, runner, _ = interactive_run(tmp_path, runner=runner)
    assert code == 0 and UNIT_PATH not in runner.installed and "Skipped" in output


# -- existing configuration -------------------------------------------------------------------------------------------


def test_existing_key_and_user_are_kept(tmp_path):
    key = generate_secret_key()
    (tmp_path / ".env").write_text(
        f"YTMUSIC_CREDENTIAL_KEY={key}\nOAUTH_DB_PATH=/old/oauth.sqlite3\n", encoding="utf-8"
    )
    code, output, _, project = interactive_run(
        tmp_path, answers=["mcp.example.com", "alice", "", ""], secrets=[PASSWORD, PASSWORD]
    )
    env = read_env(project / ".env")
    assert code == 0 and env["YTMUSIC_CREDENTIAL_KEY"] == key and "YTMUSIC_SECRET_KEY" not in env
    assert env["YTMUSIC_DB_PATH"].endswith("ytmusic-mcp.db") and "older OAUTH_DB_PATH" in output

    # Running again with the same user keeps the password unless a new one is typed.
    code, output, _, _ = interactive_run(tmp_path, answers=["mcp.example.com", "alice", "", ""], secrets=[""])
    assert code == 0 and "Kept the existing user" in output


def test_domain_defaults_to_the_one_in_an_existing_env(tmp_path):
    (tmp_path / ".env").write_text("MCP_PUBLIC_URL=https://saved.example.com/ytmusic-mcp\n", encoding="utf-8")
    code, _, _, project = interactive_run(tmp_path, answers=["", "alice", "", ""])
    assert code == 0
    assert read_env(project / ".env")["MCP_PUBLIC_URL"] == "https://saved.example.com/ytmusic-mcp"


def test_warns_about_network_disks_and_wrong_dns(tmp_path, monkeypatch):
    mounts = f"srv:/x {tmp_path.resolve()} nfs4 rw 0 0\n"
    monkeypatch.setattr(wizard, "read_text", lambda path: mounts if path == "/proc/mounts" else "")
    services = healthy_services(resolve=lambda domain: ["198.51.100.9"])
    _, output, _, _ = interactive_run(tmp_path, services=services)
    assert "network disk (nfs4)" in output
    assert "points at 198.51.100.9, but this server's address is 203.0.113.5" in output


# -- small pieces --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain, ok",
    [
        ("mcp.example.com", True),
        ("a.io", True),
        ("https://x.com", False),
        ("localhost", False),
        ("bad domain.com", False),
    ],
)
def test_domain_validation(domain, ok):
    assert (validate_domain(domain) is None) is ok


def test_username_and_password_validation():
    assert (
        validate_username("alice") is None
        and validate_username("a b") is not None
        and validate_username("x") is not None
    )
    assert validate_password("x" * 12) is None and validate_password("short") is not None


@pytest.mark.parametrize(
    "listeners, expected",
    [
        ({}, "none"),
        ({80: "caddy", 443: "caddy"}, "caddy"),
        ({443: "nginx"}, "nginx"),
        ({80: "apache2"}, "other"),
        ({80: ""}, "other"),
    ],
)
def test_detect_proxy(listeners, expected):
    assert detect_proxy(listeners) == expected


def test_rerunning_the_wizard_on_an_installed_server_no_longer_stops_at_caddy(tmp_path):
    console, _ = make_console()
    runner = caddy_machine(console)
    runner.files[CADDY_PATH] = CADDYFILE + "\n" + proxy.caddy_site_block("mcp.example.com", 8787)
    code, output, runner, _ = interactive_run(tmp_path, runner=runner)  # no --replace-site
    assert code == 0 and "right settings" in output and CADDY_PATH not in runner.installed
