from types import SimpleNamespace

from app.security.user_context import current_user_id
from app.storage import user_credentials_store as store


def test_credentials_are_isolated_by_user(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "settings", SimpleNamespace(data_dir=tmp_path))

    store.merge_user_credentials(1, {"tushare_token": "admin-token", "llm": {"api_key": "sk-admin"}})
    store.merge_user_credentials(2, {"tushare_token": "user-token", "llm": {"api_key": "sk-user"}})

    assert store.load_user_credentials(1)["tushare_token"] == "admin-token"
    assert store.load_user_credentials(2)["tushare_token"] == "user-token"
    assert store.load_user_credentials(1)["llm"]["api_key"] == "sk-admin"
    assert store.load_user_credentials(2)["llm"]["api_key"] == "sk-user"


def test_current_user_context_does_not_leak():
    assert current_user_id.get() is None
    marker = current_user_id.set(7)
    try:
        assert current_user_id.get() == 7
    finally:
        current_user_id.reset(marker)
    assert current_user_id.get() is None
