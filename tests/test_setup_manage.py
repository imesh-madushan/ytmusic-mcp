"""status / restart / update / rebuild, against a recording runner and canned services."""

import sys

import pytest
from setup_fakes import (
    CADDYFILE,
    PLAIN_SS_BEFORE,
    SS_CADDY,
    FakeRunner,
    healthy_services,
    make_console,
)

from ytmusic_mcp.cli import build_parser
from ytmusic_mcp.setup import manage, proxy
from ytmusic_mcp.setup.manage import ManageOptions, Manager
from ytmusic_mcp.setup.proxy import Check
from ytmusic_mcp.setup.system import Result

CADDY_PATH = "/etc/caddy/Caddyfile"
UNIT_PATH = "/etc/systemd/system/ytmusic-mcp.service"
ENV = "MCP_PUBLIC_URL=https://mcp.example.com/ytmusic-mcp\nMCP_PORT=8787\n"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(proxy.time, "sleep", lambda seconds: None)


@pytest.fixture
def project(tmp_path):
    """A set-up installation: .env plus the two Vite apps' package.json files."""
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    for app in manage.UI_APPS:
        (tmp_path / "widgets" / app).mkdir(parents=True)
        (tmp_path / "widgets" / app / "package.json").write_text("{}", encoding="utf-8")
    return tmp_path


def git(project, *args):
    return ("git", "-C", str(project), *args)


def sequence(*results):
    """A response that gives the next Result on every call (the last one repeats)."""
    items = list(results)
    return lambda: items.pop(0) if len(items) > 1 else items[0]


def make_runner(project, responses=None, **kwargs):
    console, out = make_console()
    base = {
        ("ss", "-tlnp"): Result(stdout=SS_CADDY),
        ("ss", "-tln"): Result(stdout=PLAIN_SS_BEFORE),
        git(project, "rev-parse", "--is-inside-work-tree"): Result(stdout="true\n"),
        git(project, "rev-parse", "--abbrev-ref", "--symbolic-full-name"): Result(stdout="origin/deoply\n"),
        git(project, "rev-parse", "--abbrev-ref", "HEAD"): Result(stdout="deoply\n"),
        git(project, "rev-parse", "--short", "HEAD"): Result(stdout="abc1234\n"),
        git(project, "log", "-1"): Result(stdout="Add a thing\n"),
        git(project, "status", "--porcelain"): Result(stdout=""),
        git(project, "rev-list"): Result(stdout="0\t0\n"),
    }
    base.update(responses or {})
    kwargs.setdefault("tools", ("systemctl", "caddy", "npm", "nginx", "certbot"))
    runner = FakeRunner(console, responses=base, files={CADDY_PATH: CADDYFILE}, **kwargs)
    return runner, console, out


def manager(project, runner, console, services=None, **options):
    return Manager(
        ManageOptions(project_dir=project, **options), console, runner, services or healthy_services()
    )


# -- status ---------------------------------------------------------------------------------------


def test_status_healthy(project):
    runner, console, out = make_runner(project)
    assert manager(project, runner, console).status() == 0
    text = out.getvalue()
    assert "https://mcp.example.com/ytmusic-mcp" in text and "deoply @ abc1234" in text
    assert "Web server: Caddy" in text and "Everything looks healthy" in text
    assert not any(call[0] == "git" and call[3] in ("fetch", "pull") for call in runner.executed)  # offline


def test_status_reports_a_stopped_service_and_failing_public_checks(project):
    runner, console, out = make_runner(project, {("systemctl", "is-active"): Result(3, stdout="inactive\n")})
    failing = healthy_services(
        check_public=lambda domain: [Check("MCP endpoint asks for an OAuth token", False, "HTTP 502")]
    )
    code = manager(project, runner, console, failing).status()
    assert code == 1
    assert "Service ytmusic-mcp: inactive" in out.getvalue() and "HTTP 502" in out.getvalue()
    assert "journalctl" in out.getvalue()


def test_commands_refuse_when_the_server_is_not_set_up(tmp_path):
    runner, console, out = make_runner(tmp_path)
    assert manager(tmp_path, runner, console).status() == 1
    assert "not set up yet" in out.getvalue() and "install.sh" in out.getvalue()


def test_commands_explain_when_not_on_linux(project):
    runner, console, out = make_runner(project)
    assert manager(project, runner, console, healthy_services(system=lambda: "Darwin")).restart() == 1
    assert "Linux servers" in out.getvalue()


# -- restart ---------------------------------------------------------------------------------------------


def test_restart_restarts_and_checks(project):
    runner, console, out = make_runner(project)
    assert manager(project, runner, console).restart() == 0
    assert runner.did("systemctl", "restart", "ytmusic-mcp") and "Restarted" in out.getvalue()


def test_restart_reports_a_service_that_does_not_come_back(project):
    runner, console, out = make_runner(project)
    dead = healthy_services(check_local=lambda port: Check("App", False, "no response"))
    assert manager(project, runner, console, dead).restart() == 1
    assert "did not start" in out.getvalue()


# -- update: the quick path ------------------------------------------------------------------------------------


def behind_by_two(project, extra=None):
    responses = {
        git(project, "rev-parse", "--short", "HEAD"): sequence(
            Result(stdout="abc1234\n"), Result(stdout="def5678\n")
        ),
        git(project, "rev-list"): Result(stdout="0\t2\n"),
        git(project, "log", "--format=%h %s"): Result(stdout="def5678 Second\nbbb2222 First\n"),
        git(project, "diff", "--name-only"): Result(stdout="ytmusic_mcp/tools.py\nREADME.md\n"),
    }
    responses.update(extra or {})
    return responses


def test_update_pulls_installs_restarts_and_checks_in_order(project):
    runner, console, out = make_runner(project, behind_by_two(project))
    assert manager(project, runner, console).update() == 0

    pip = (sys.executable, "-m", "pip", "install")
    order = [
        runner.position(*git(project, "fetch")),
        runner.position(*git(project, "pull", "--ff-only")),
        runner.position(*pip),
        runner.position("systemctl", "restart", "ytmusic-mcp"),
    ]
    assert order == sorted(order)
    text = out.getvalue()
    assert "2 new commits" in text and "def5678 Second" in text and "Updated abc1234 -> def5678" in text
    assert "refresh or re-add the connector" in text  # tools.py changed
    assert (
        CADDY_PATH not in runner.installed and UNIT_PATH not in runner.installed
    )  # the quick path touches no config


def test_update_points_to_rebuild_when_ui_source_or_deploy_files_changed(project):
    changed = "widgets/oauth-login/src/main.tsx\ndeploy/caddy-site.template\n"
    runner, console, out = make_runner(
        project, behind_by_two(project, {git(project, "diff", "--name-only"): Result(stdout=changed)})
    )
    assert manager(project, runner, console).update() == 0
    assert out.getvalue().count("install.sh rebuild") >= 2


def test_update_does_nothing_when_already_current(project):
    runner, console, out = make_runner(project)
    assert manager(project, runner, console).update() == 0
    assert "Already up to date at abc1234" in out.getvalue()
    assert not runner.did(*git(project, "pull")) and not runner.did("systemctl", "restart")
    assert not runner.did(sys.executable)


def test_update_refuses_local_changes_and_pulls_nothing(project):
    runner, console, out = make_runner(
        project, {git(project, "status", "--porcelain"): Result(stdout=" M ytmusic_mcp/app.py\n")}
    )
    assert manager(project, runner, console).update() == 1
    assert "local changes (ytmusic_mcp/app.py)" in out.getvalue()
    assert not runner.did(*git(project, "fetch")) and not runner.did(*git(project, "pull"))


def test_update_restores_locally_rebuilt_ui_files_instead_of_refusing(project):
    dirty = " M widgets/playlist-manager/dist/index.html\n"
    runner, console, _ = make_runner(
        project, behind_by_two(project, {git(project, "status", "--porcelain"): Result(stdout=dirty)})
    )
    assert manager(project, runner, console).update() == 0
    assert runner.did(*git(project, "checkout", "--", "widgets/playlist-manager/dist/index.html"))


def test_update_stops_cleanly_when_history_has_diverged(project):
    runner, console, out = make_runner(project, {git(project, "rev-list"): Result(stdout="1\t2\n")})
    assert manager(project, runner, console).update() == 1
    assert "diverged" in out.getvalue() and "git reset --hard origin/deoply" in out.getvalue()
    assert not runner.did(*git(project, "pull"))


@pytest.mark.parametrize(
    "response, message",
    [
        ({git("", "rev-parse", "--is-inside-work-tree"): Result(1)}, "not a git checkout"),
        (
            {("git", "-C", "", "rev-parse", "--abbrev-ref", "--symbolic-full-name"): Result(1)},
            "does not follow a remote branch",
        ),
    ],
)
def test_update_explains_git_setup_problems(project, response, message):
    fixed = {
        tuple(str(project) if part == "" else part for part in key): value for key, value in response.items()
    }
    runner, console, out = make_runner(project, fixed)
    assert manager(project, runner, console).update() == 1
    assert message in out.getvalue()


def test_update_reports_network_and_fast_forward_failures(project):
    runner, console, out = make_runner(project, {git(project, "fetch"): Result(1)})
    assert manager(project, runner, console).update() == 1 and "Could not reach the remote" in out.getvalue()

    runner, console, out = make_runner(
        project, behind_by_two(project, {git(project, "pull"): Result(1, stderr="not possible")})
    )
    assert manager(project, runner, console).update() == 1 and "could not fast-forward" in out.getvalue()


def test_update_stops_before_restarting_when_packages_fail(project):
    runner, console, out = make_runner(
        project,
        behind_by_two(project, {(sys.executable, "-m", "pip"): Result(1, stderr="no matching distribution")}),
    )
    assert manager(project, runner, console).update() == 1
    assert "no matching distribution" in out.getvalue() and not runner.did("systemctl", "restart")


def test_update_no_restart_leaves_the_service_alone(project):
    runner, console, out = make_runner(project, behind_by_two(project))
    assert manager(project, runner, console, no_restart=True).update() == 0
    assert not runner.did("systemctl", "restart") and "Restart when ready" in out.getvalue()


def test_update_dry_run_executes_nothing_that_changes_anything(project):
    runner, console, out = make_runner(project, behind_by_two(project))
    assert manager(project, runner, console, dry_run=True).update() == 0
    for prefix in (git(project, "fetch"), git(project, "pull"), (sys.executable,), ("systemctl", "restart")):
        assert not runner.did(*prefix)
    assert "Would fast-forward" in out.getvalue() and "Dry run" in out.getvalue()


# -- rebuild: the full path ----------------------------------------------------------------------------------------


def only_oauth_login_is_uncommitted(project):
    return {git(project, "ls-files", "--error-unmatch", "widgets/oauth-login/dist/index.html"): Result(1)}


def npm(project, app, *args):
    return ("npm", "--prefix", str(project / "widgets" / app), *args)


def test_rebuild_reapplies_everything_in_order(project):
    runner, console, out = make_runner(project, only_oauth_login_is_uncommitted(project))
    assert manager(project, runner, console).rebuild() == 0

    steps = [
        runner.position(sys.executable, "-m", "pip"),
        runner.position(*npm(project, "oauth-login", "ci")),
        runner.position(*npm(project, "oauth-login", "run", "build")),
        runner.position("systemctl", "daemon-reload"),
        runner.position("systemctl", "reload", "caddy"),
        runner.position("systemctl", "restart", "ytmusic-mcp"),
    ]
    assert steps == sorted(steps)
    assert not runner.did(
        *npm(project, "playlist-manager", "ci")
    )  # its build is committed, so it is used as is
    assert UNIT_PATH in runner.installed and "mcp.example.com {" in runner.installed[CADDY_PATH]
    assert "Rebuilt" in out.getvalue() and "build is committed to git" in out.getvalue()


def test_rebuild_all_ui_builds_the_committed_ones_too(project):
    runner, console, _ = make_runner(project)
    assert manager(project, runner, console, build_all_ui=True).rebuild() == 0
    assert runner.did(*npm(project, "playlist-manager", "run", "build")) and runner.did(
        *npm(project, "oauth-login", "run", "build")
    )


def test_rebuild_can_skip_the_ui(project):
    runner, console, out = make_runner(project, only_oauth_login_is_uncommitted(project))
    assert manager(project, runner, console, skip_ui=True).rebuild() == 0
    assert not runner.did("npm") and "Skipped (--skip-ui)" in out.getvalue()


def test_rebuild_without_node_still_succeeds_and_says_why(project):
    runner, console, out = make_runner(
        project, only_oauth_login_is_uncommitted(project), tools=("systemctl", "caddy")
    )
    assert manager(project, runner, console).rebuild() == 0
    assert "Node.js (npm) is not installed" in out.getvalue() and UNIT_PATH in runner.installed


def test_failed_ui_build_stops_before_touching_the_service(project):
    failing = {
        **only_oauth_login_is_uncommitted(project),
        npm(project, "oauth-login", "run", "build"): Result(1),
    }
    runner, console, out = make_runner(project, failing)
    assert manager(project, runner, console).rebuild() == 1
    assert "build of oauth-login failed" in out.getvalue() and "npm --prefix" in out.getvalue()
    assert UNIT_PATH not in runner.installed and not runner.did("systemctl", "restart")


def test_rebuild_warns_before_a_memory_hungry_build(project, monkeypatch):
    monkeypatch.setattr(manage, "read_text", lambda path: "MemTotal: 975000 kB\nSwapTotal: 0 kB\n")
    runner, console, out = make_runner(project, only_oauth_login_is_uncommitted(project))
    assert manager(project, runner, console).rebuild() == 0
    assert "no swap" in out.getvalue()


def test_rebuild_leaves_other_peoples_caddy_sites_alone(project):
    runner, console, out = make_runner(project)
    runner.files[CADDY_PATH] = CADDYFILE.replace("other.example.com", "mcp.example.com")
    assert manager(project, runner, console).rebuild() == 1
    assert "already used by another Caddy site" in out.getvalue() and CADDY_PATH not in runner.installed


def test_rebuild_updates_its_own_older_caddy_block_without_asking(project):
    runner, console, _ = make_runner(project)
    runner.files[CADDY_PATH] = CADDYFILE + "\nmcp.example.com {\n\t@mcp path /ytmusic-mcp\n\trespond 200\n}\n"
    assert manager(project, runner, console).rebuild() == 0
    new = runner.installed[CADDY_PATH]
    assert "reverse_proxy 127.0.0.1:8787" in new and "respond 200" not in new
    assert "http://100.64.0.10:8080 {" in new and "other.example.com {" in new  # everything else untouched


def test_rebuild_leaves_an_identical_caddy_block_alone(project):
    runner, console, out = make_runner(project)
    runner.files[CADDY_PATH] = CADDYFILE + "\n" + proxy.caddy_site_block("mcp.example.com", 8787)
    assert manager(project, runner, console).rebuild() == 0
    assert (
        CADDY_PATH not in runner.installed
        and "already serves mcp.example.com with the right settings" in out.getvalue()
    )


def test_rebuild_skips_web_servers_it_does_not_manage(project):
    runner, console, out = make_runner(
        project, {("ss", "-tlnp"): Result(stdout='LISTEN 0 511 *:443 *:* users:(("apache2",pid=9,fd=4))\n')}
    )
    assert manager(project, runner, console).rebuild() == 0
    assert "not managed by this tool" in out.getvalue() and CADDY_PATH not in runner.installed


def test_rebuild_with_nginx_reapplies_the_nginx_site(project):
    runner, console, _ = make_runner(project)
    assert manager(project, runner, console, proxy="nginx").rebuild() == 0
    assert runner.did("nginx", "-t") and proxy.NGINX_SITE in runner.installed


def test_rebuild_dry_run_changes_nothing(project):
    runner, console, out = make_runner(project, only_oauth_login_is_uncommitted(project))
    assert manager(project, runner, console, dry_run=True).rebuild() == 0
    assert runner.installed == {} and not runner.did("npm") and not runner.did("systemctl", "restart")
    assert "Dry run" in out.getvalue()


# -- command line -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, attribute, value",
    [
        (["update", "--dry-run"], "dry_run", True),
        (["update", "--no-restart"], "no_restart", True),
        (["rebuild", "--skip-ui"], "skip_ui", True),
        (["rebuild", "--build-all-ui"], "build_all_ui", True),
        (["rebuild", "--proxy", "nginx"], "proxy", "nginx"),
    ],
)
def test_command_line_options(argv, attribute, value):
    args = build_parser().parse_args(argv)
    assert getattr(args, attribute) == value and args.command == argv[0]


def test_every_maintenance_command_is_registered():
    parser = build_parser()
    for command in ("status", "restart", "update", "rebuild"):
        assert parser.parse_args([command]).command == command
