from app.storage import profile_store, watchlist_store


def test_saving_watchlist_retains_other_users_company_profiles():
    watchlist_store.save_symbols(1, ["600519.SS"])
    profile_store.patch_manual("600519.SS", {"intro": "管理员维护的公司资料"})
    watchlist_store.save_symbols(2, ["000001.SZ"])
    profile_store.patch_manual("000001.SZ", {"intro": "另一个账户维护的资料"})

    assert profile_store.get_entry("600519.SS")["manual"]["intro"] == "管理员维护的公司资料"
    watchlist_store.save_symbols(1, [])

    assert profile_store.get_entry("000001.SZ")["manual"]["intro"] == "另一个账户维护的资料"
    assert profile_store.get_entry("600519.SS")["manual"] == {}


def test_invalid_other_watchlist_does_not_break_save(tmp_path):
    watchlist_store.save_symbols(1, ["600519.SS"])
    (tmp_path / "watchlists" / "2.json").write_text("{invalid json", encoding="utf-8")
    assert watchlist_store.save_symbols(1, ["000001.SZ"]) == ["000001.SZ"]
