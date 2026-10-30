import pytest

from ytmusic_mcp.security import (
    CookieVault,
    VaultError,
    generate_secret_key,
    hash_password,
    hash_token,
    verify_password,
)


def test_password_hash_verifies_and_is_salted():
    first, second = hash_password("correct horse"), hash_password("correct horse")
    assert first != second
    assert verify_password("correct horse", first)
    assert not verify_password("wrong", first)


def test_verify_password_rejects_garbage():
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "md5$1$2$3$4$5")


def test_hash_token_is_stable_and_not_the_token():
    assert hash_token("secret") == hash_token("secret")
    assert "secret" not in hash_token("secret")


def test_vault_round_trip(vault: CookieVault):
    blob = vault.encrypt("cookie=value")
    assert b"cookie=value" not in blob
    assert vault.decrypt(blob) == "cookie=value"


def test_vault_with_changed_key_fails_clearly(vault: CookieVault):
    blob = vault.encrypt("cookie=value")
    with pytest.raises(VaultError, match="YTMUSIC_SECRET_KEY has changed"):
        CookieVault(generate_secret_key()).decrypt(blob)


def test_vault_rejects_invalid_key():
    with pytest.raises(VaultError):
        CookieVault("not a fernet key")
