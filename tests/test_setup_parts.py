"""The setup wizard's building blocks: terminal UI, parsers, .env editing, Caddy/nginx logic."""

import io

import pytest
from setup_fakes import CADDYFILE, PLAIN_SS_BEFORE, SS_CADDY, FakeRunner, make_console

from ytmusic_mcp.setup import proxy
from ytmusic_mcp.setup.envfile import read_env, set_env
from ytmusic_mcp.setup.proxy import StepFailed
from ytmusic_mcp.setup.system import Result, filesystem_of, memory_mb, parse_listeners
from ytmusic_mcp.setup.ui import Console

CADDY_PATH = "/etc/caddy/Caddyfile"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(proxy.time, "sleep", lambda seconds: None)


# -- terminal UI ------------------------------------------------------------------------------


def test_ask_retries_until_valid_and_uses_the_default():
    console, out = make_console(answers=["", "bad", "good"])
    only_good = lambda value: None if value == "good" else "Not good enough."
    assert console.ask("Pick", validate=only_good) == "good"
    assert out.getvalue().count("Not good enough.") == 2  # the empty answer and "bad"

    console, _ = make_console(answers=[""])
    assert console.ask("Pick", default="fallback") == "fallback"


def test_confirm_uses_its_default_and_rejects_nonsense():
    console, out = make_console(answers=["", "maybe", "n"])
    assert console.confirm("Go?", default=True) is True
    assert console.confirm("Go?", default=True) is False
    assert "Please answer y or n." in out.getvalue()


def test_output_has_no_colour_codes_when_piped_or_disabled(monkeypatch):
    console = Console(out=io.StringIO())
    assert console.color is False

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert Console(out=Terminal()).color is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert Console(out=Terminal()).color is False


def test_ascii_fallback_without_unicode_and_colour_when_enabled():
    out = io.StringIO()
    console = Console(out=out, color=False, unicode=False, width=40)
    console.header("Title", "Sub")
    console.ok("fine")
    text = out.getvalue()
    assert "+" in text and "│" not in text and "OK fine" in text

    coloured = io.StringIO()
    Console(out=coloured, color=True, unicode=True).ok("fine")
    assert "\033[" in coloured.getvalue()


def test_table_aligns_labels():
    console, out = make_console()
    console.table([("A", "1"), ("Longer", "2")])
    assert "A       1" in out.getvalue()


# -- parsers ---------------------------------------------------------------------------------


def test_parse_listeners_reads_ports_and_process_names():
    assert parse_listeners(SS_CADDY) == {80: "caddy", 443: "caddy"}
    assert parse_listeners(PLAIN_SS_BEFORE) == {80: "", 443: ""}
    assert parse_listeners("State Recv-Q\nLISTEN 0 4096 127.0.0.1:8787 0.0.0.0:*") == {8787: ""}


def test_filesystem_of_picks_the_most_specific_mount():
    mounts = "/dev/sda1 / ext4 rw 0 0\nsrv:/x /srv/shared nfs4 rw 0 0\n"
    assert filesystem_of("/srv/shared/Projects", mounts) == "nfs4"
    assert filesystem_of("/srv/other", mounts) == "ext4"
    assert filesystem_of("/srv/shared-extra", mounts) == "ext4"  # not inside the mount


def test_memory_mb_reads_total_and_swap():
    meminfo = "MemTotal:  975000 kB\nSwapTotal: 1048576 kB\n"
    assert memory_mb(meminfo) == (952, 1024)
    assert memory_mb("") == (0, 0)


# -- .env editing -------------------------------------------------------------------------------


def test_set_env_updates_in_place_keeps_comments_and_appends(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comment\nMCP_PORT=1111\n# OTHER=example\nKEEP=yes\n", encoding="utf-8")
    set_env(path, {"MCP_PORT": "8787", "NEW": "value"})
    assert path.read_text(encoding="utf-8").splitlines() == [
        "# comment",
        "MCP_PORT=8787",
        "# OTHER=example",
        "KEEP=yes",
        "",
        "NEW=value",
    ]
    assert read_env(path) == {"MCP_PORT": "8787", "KEEP": "yes", "NEW": "value"}


def test_set_env_starts_from_the_template(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text("# Example\nMCP_PORT=8787\n", encoding="utf-8")
    path = tmp_path / ".env"
    set_env(path, {"MCP_PORT": "9000"}, template=template)
    assert path.read_text(encoding="utf-8").startswith("# Example")
    assert read_env(path)["MCP_PORT"] == "9000"


# -- Caddyfile parsing ------------------------------------------------------------------------------


def test_top_level_blocks_and_find_site():
    blocks = proxy.top_level_blocks(CADDYFILE)
    assert [b.addresses for b in blocks] == [["http://100.64.0.10:8080"], ["other.example.com"]]
    assert proxy.find_site(CADDYFILE, "OTHER.example.com") is not None
    assert proxy.find_site(CADDYFILE, "100.64.0.10:8080") is not None  # the http:// scheme is ignored
    assert proxy.find_site(CADDYFILE, "100.64.0.10") is None  # a different port is a different site
    assert proxy.find_site(CADDYFILE, "mcp.example.com") is None


def test_global_options_block_is_not_a_site():
    text = "{\n\temail me@example.com\n}\n\nsite.example.com {\n\trespond ok\n}\n"
    assert [b.addresses for b in proxy.top_level_blocks(text)] == [["site.example.com"]]


def test_with_site_adds_or_replaces_without_touching_other_sites():
    block = proxy.caddy_site_block("mcp.example.com", 8787)
    added = proxy.with_site(CADDYFILE, "mcp.example.com", block, replace=False)
    assert added.startswith(CADDYFILE.rstrip("\n")) and "mcp.example.com {" in added

    replaced = proxy.with_site(
        CADDYFILE, "other.example.com", block.replace("mcp.example.com", "other.example.com"), replace=True
    )
    assert "fileuser" not in replaced and "http://100.64.0.10:8080 {" in replaced
    assert replaced.count("other.example.com {") == 1


def test_caddy_block_is_fully_filled_in():
    block = proxy.caddy_site_block("mcp.example.com", 9001)
    assert block.startswith("mcp.example.com {") and "127.0.0.1:9001" in block
    assert "${" not in block and "respond 404" in block


def test_mask_secrets_hides_password_hashes():
    assert "abcdef" not in proxy.mask_secrets(
        "user $2a$14$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ0123"
    )
    assert "<hash hidden>" in proxy.mask_secrets("user $2b$10$abcdefghijklmnopqrstuu")


# -- configure_caddy: safe changes ------------------------------------------------------------------------


def caddy_runner(console, **kwargs):
    responses = {("ss", "-tln"): Result(stdout=PLAIN_SS_BEFORE), **kwargs.pop("responses", {})}
    return FakeRunner(console, files={CADDY_PATH: CADDYFILE}, responses=responses, **kwargs)


def test_new_site_is_added_after_a_backup_and_validation():
    console, _ = make_console()
    runner = caddy_runner(console)
    backup = proxy.configure_caddy(runner, console, "mcp.example.com", 8787, replace_existing=None)

    assert backup.startswith(CADDY_PATH + ".bak-")
    names = [call[0] for call in runner.calls]
    reload_at = next(i for i, call in enumerate(runner.calls) if call[:3] == ("systemctl", "reload", "caddy"))
    assert (
        names.index("caddy") < names.index("cp") < names.index("install") < reload_at
    )  # validate, back up, apply, reload
    assert runner.ran("cp", "-p", CADDY_PATH, backup)
    new = runner.installed[CADDY_PATH]
    assert "mcp.example.com {" in new and "http://100.64.0.10:8080 {" in new and "fileuser" in new


def test_existing_site_is_not_replaced_without_consent_and_secrets_stay_hidden():
    console, out = make_console()
    runner = caddy_runner(console)
    with pytest.raises(StepFailed, match="already used"):
        proxy.configure_caddy(runner, console, "other.example.com", 8787, replace_existing=False)
    assert runner.installed == {} and not runner.ran("cp")
    assert "<hash hidden>" in out.getvalue() and "abcdefghijkl" not in out.getvalue()


def test_existing_site_is_replaced_when_allowed():
    console, _ = make_console()
    runner = caddy_runner(console)
    proxy.configure_caddy(runner, console, "other.example.com", 8787, replace_existing=True)
    new = runner.installed[CADDY_PATH]
    assert "fileuser" not in new and "127.0.0.1:8787" in new and "http://100.64.0.10:8080 {" in new


def test_existing_site_asks_when_unspecified():
    console, _ = make_console(answers=["y"])
    runner = caddy_runner(console)
    proxy.configure_caddy(runner, console, "other.example.com", 8787, replace_existing=None)
    assert "other.example.com {" in runner.installed[CADDY_PATH]


def test_rejected_config_changes_nothing():
    console, _ = make_console()
    runner = caddy_runner(console, responses={("caddy", "validate"): Result(1, stderr="unknown directive")})
    with pytest.raises(StepFailed, match="nothing was changed"):
        proxy.configure_caddy(runner, console, "mcp.example.com", 8787, replace_existing=None)
    assert runner.installed == {} and not runner.ran("cp")


def test_failed_reload_restores_the_previous_config():
    console, _ = make_console()
    runner = caddy_runner(console, responses={("systemctl", "reload", "caddy"): Result(1)})
    with pytest.raises(StepFailed, match="restored"):
        proxy.configure_caddy(runner, console, "mcp.example.com", 8787, replace_existing=None)
    assert runner.install_order == [CADDY_PATH, CADDY_PATH]
    assert runner.installed[CADDY_PATH] == CADDYFILE


def test_lost_listening_ports_trigger_a_rollback():
    console, _ = make_console()
    snapshots = iter([Result(stdout=PLAIN_SS_BEFORE), Result(stdout="LISTEN 0 4096 *:80 *:*\n")])
    runner = caddy_runner(console, responses={("ss", "-tln"): lambda: next(snapshots)})
    with pytest.raises(StepFailed, match="no longer listening"):
        proxy.configure_caddy(runner, console, "mcp.example.com", 8787, replace_existing=None)
    assert runner.installed[CADDY_PATH] == CADDYFILE


def test_caddy_not_running_after_reload_triggers_a_rollback():
    console, _ = make_console()
    runner = caddy_runner(console, responses={("systemctl", "is-active"): Result(3, stdout="failed\n")})
    with pytest.raises(StepFailed, match="not running"):
        proxy.configure_caddy(runner, console, "mcp.example.com", 8787, replace_existing=None)
    assert runner.installed[CADDY_PATH] == CADDYFILE


def test_missing_caddyfile_is_a_clear_error():
    console, _ = make_console()
    runner = FakeRunner(console)
    with pytest.raises(StepFailed, match="Cannot read"):
        proxy.configure_caddy(runner, console, "mcp.example.com", 8787, replace_existing=None)


# -- configure_nginx ---------------------------------------------------------------------------------------


def test_nginx_gets_the_certificate_before_the_config():
    console, _ = make_console()
    runner = FakeRunner(
        console, responses={("test", "-f"): Result(1)}, tools=("nginx", "certbot", "systemctl")
    )
    proxy.configure_nginx(runner, console, "mcp.example.com", 8787)
    certbot = next(i for i, call in enumerate(runner.calls) if call[0] == "certbot")
    install = next(i for i, call in enumerate(runner.calls) if call[0] == "install")
    assert certbot < install
    assert runner.ran("nginx", "-t") and runner.ran("systemctl", "reload", "nginx")
    assert (
        "listen 443 ssl http2;" in runner.installed[proxy.NGINX_SITE]
        and "mcp.example.com" in runner.installed[proxy.NGINX_SITE]
    )


def test_nginx_failure_removes_the_new_site():
    console, _ = make_console()
    runner = FakeRunner(console, responses={("nginx", "-t"): Result(1)})
    with pytest.raises(StepFailed, match="restored"):
        proxy.configure_nginx(runner, console, "mcp.example.com", 8787)
    assert runner.ran("rm", "-f", proxy.NGINX_LINK, proxy.NGINX_SITE)


def test_certificate_failure_stops_before_touching_nginx():
    console, _ = make_console()
    runner = FakeRunner(console, responses={("test", "-f"): Result(1), ("certbot",): Result(1)})
    with pytest.raises(StepFailed, match="certificate"):
        proxy.configure_nginx(runner, console, "mcp.example.com", 8787)
    assert runner.installed == {}


# -- checks against the running site ----------------------------------------------------------------------------


def test_public_checks_judge_each_response(monkeypatch):
    answers = {
        "/ytmusic-mcp": (401, {"www-authenticate": 'Bearer error="invalid_token"'}),
        "/.well-known/oauth-authorization-server": (200, {}),
    }

    def fake_fetch(url, timeout=15.0):
        return answers.get(url.split("example.com", 1)[1], (404, {}))

    monkeypatch.setattr(proxy, "fetch", fake_fetch)
    assert all(check.ok for check in proxy.check_public("mcp.example.com"))

    answers["/ytmusic-mcp"] = (401, {"www-authenticate": 'Basic realm="restricted"'})  # someone else's login
    results = proxy.check_public("mcp.example.com")
    assert [check.ok for check in results] == [False, True, True]


def test_every_forwarded_path_appears_in_the_caddy_template():
    block = proxy.caddy_site_block("mcp.example.com", 8787)
    assert all(path in block for path in proxy.OAUTH_PATHS)


def test_non_linux_systems_are_told_what_to_do_instead(tmp_path):
    from setup_fakes import healthy_services, make_options

    from ytmusic_mcp.setup.wizard import Wizard

    console, out = make_console()
    runner = FakeRunner(console)
    wizard_run = Wizard(
        make_options(tmp_path), console, runner, healthy_services(system=lambda: "Darwin")
    ).execute()
    assert wizard_run == 1 and "for Linux servers" in out.getvalue() and "local-cookies" in out.getvalue()
