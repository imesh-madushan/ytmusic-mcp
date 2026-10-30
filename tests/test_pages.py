"""The login pages: the built React app when present, otherwise server-rendered HTML."""

import json

import pytest

from ytmusic_mcp import pages

BUILT_PAGE = (
    '<!doctype html><script>window.__OAUTH_PAGE_DATA__="__OAUTH_PAGE_DATA__"</script><div id="root"></div>'
)


@pytest.fixture
def no_react_build(monkeypatch, tmp_path):
    monkeypatch.setattr(pages, "OAUTH_UI_PATH", tmp_path / "missing.html")


@pytest.fixture
def react_build(monkeypatch, tmp_path):
    path = tmp_path / "index.html"
    path.write_text(BUILT_PAGE, encoding="utf-8")
    monkeypatch.setattr(pages, "OAUTH_UI_PATH", path)


def test_plain_login_page_has_the_form_fields_and_names_the_client(no_react_build):
    page = pages.render_page("login", state="abc", client_name="Claude", redirect_host="claude.ai")
    body = page.body.decode()
    assert 'action="/login/callback"' in body
    assert 'name="state" value="abc"' in body
    assert "Claude" in body and "claude.ai" in body
    assert "script-src" not in page.headers["content-security-policy"]


def test_plain_youtube_page_posts_headers_with_state_or_ticket(no_react_build):
    body = pages.render_page("youtube", state="abc", ticket="t1").body.decode()
    assert 'action="/youtube-auth/callback"' in body
    assert 'name="headers"' in body
    assert 'name="ticket" value="t1"' in body


def test_plain_pages_escape_untrusted_text(no_react_build):
    page = pages.render_page("login", state="x", client_name="<script>alert(1)</script>", error="<b>bad</b>")
    body = page.body.decode()
    assert "<script>alert(1)</script>" not in body
    assert "<b>bad</b>" not in body


def test_react_build_gets_its_page_data_injected(react_build):
    page = pages.render_page(
        "youtube", state="abc", ticket="t1", client_name="Claude", redirect_host="claude.ai", error="oops"
    )
    body = page.body.decode()
    assert pages.PAGE_DATA_PLACEHOLDER not in body
    data = json.loads(body.split("window.__OAUTH_PAGE_DATA__=")[1].split("</script>")[0])
    assert data == {
        "state": "abc",
        "ticket": "t1",
        "mode": "youtube",
        "clientName": "Claude",
        "redirectHost": "claude.ai",
        "error": "oops",
    }
    assert "script-src 'unsafe-inline'" in page.headers["content-security-policy"]


def test_react_page_data_cannot_break_out_of_the_script_tag(react_build):
    body = pages.render_page("login", client_name="</script><script>alert(1)</script>").body.decode()
    assert "</script><script>alert(1)" not in body


@pytest.mark.parametrize("make_page", [lambda: pages.render_page("login"), lambda: pages.message_page("x")])
def test_pages_forbid_framing_and_caching(no_react_build, make_page):
    headers = make_page().headers
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
