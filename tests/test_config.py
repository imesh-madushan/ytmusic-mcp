import pytest

from ytmusic_mcp.config import ConfigError, settings_from_env


def http_env(**overrides):
    env = {
        "MCP_TRANSPORT": "http",
        "MCP_PUBLIC_URL": "https://mcp.example.com/ytmusic-mcp/",
        "YTMUSIC_SECRET_KEY": "key",
    }
    return {**env, **overrides}


def test_defaults_are_stdio():
    settings = settings_from_env({})
    assert settings.transport == "stdio"
    assert settings.host == "127.0.0.1"
    assert settings.static_client is None


def test_issuer_is_the_origin_of_the_public_url():
    settings = settings_from_env(http_env())
    assert settings.public_url == "https://mcp.example.com/ytmusic-mcp"
    assert settings.issuer_url == "https://mcp.example.com"
    settings.validate_for_http()


def test_http_requires_secret_key_and_public_url():
    with pytest.raises(ConfigError, match="YTMUSIC_SECRET_KEY"):
        settings_from_env(http_env(YTMUSIC_SECRET_KEY="")).validate_for_http()
    with pytest.raises(ConfigError, match="MCP_PUBLIC_URL"):
        settings_from_env(http_env(MCP_PUBLIC_URL="")).validate_for_http()


def test_http_requires_https_except_on_localhost():
    with pytest.raises(ConfigError, match="https"):
        settings_from_env(http_env(MCP_PUBLIC_URL="http://mcp.example.com/mcp")).validate_for_http()
    settings_from_env(http_env(MCP_PUBLIC_URL="http://localhost:8787/mcp")).validate_for_http()


def test_invalid_transport_and_port():
    with pytest.raises(ConfigError):
        settings_from_env({"MCP_TRANSPORT": "carrier-pigeon"})
    with pytest.raises(ConfigError):
        settings_from_env({"MCP_PORT": "eighty"})


def test_static_client_needs_all_three_values():
    with pytest.raises(ConfigError, match="must all be set"):
        settings_from_env({"OAUTH_CLIENT_ID": "id"})
    settings = settings_from_env(
        {
            "OAUTH_CLIENT_ID": "id",
            "OAUTH_CLIENT_SECRET": "s",
            "OAUTH_REDIRECT_URIS": "https://a/cb, https://b/cb",
        }
    )
    assert settings.static_client.redirect_uris == ["https://a/cb", "https://b/cb"]


def test_older_environment_variable_names_are_still_accepted():
    settings = settings_from_env(
        {"YTMUSIC_CREDENTIAL_KEY": "old-key", "OAUTH_DB_PATH": "/data/oauth.sqlite3"}
    )
    assert settings.secret_key == "old-key"
    assert settings.db_path.name == "oauth.sqlite3"
    # The new names win when both are present.
    both = settings_from_env({"YTMUSIC_SECRET_KEY": "new", "YTMUSIC_CREDENTIAL_KEY": "old"})
    assert both.secret_key == "new"


def test_force_ipv4_defaults_on_and_can_be_disabled():
    assert settings_from_env({}).force_ipv4 is True
    assert settings_from_env({"MCP_FORCE_IPV4": "0"}).force_ipv4 is False
