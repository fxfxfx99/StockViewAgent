from app.storage import profile_store


def test_failed_company_refresh_retains_last_successful_profile():
    profile_store.upsert_auto("600519.SS", {"name": "贵州茅台", "intro": "原有公司资料"}, None)
    profile_store.patch_manual("600519.SS", {"industry": "手动行业"})
    before = profile_store.get_entry("600519.SS")

    profile_store.upsert_auto("600519.SS", {}, "上游接口暂不可用")
    after = profile_store.get_entry("600519.SS")

    assert after["auto"] == before["auto"]
    assert after["manual"] == before["manual"]
    assert after["em_fetched_at"] == before["em_fetched_at"]
    assert after["fetch_error"] == "上游接口暂不可用"


def test_failed_tushare_refresh_retains_cached_data_and_timestamp():
    profile_store.upsert_tushare("600519.SS", {"basic": {"name": "贵州茅台"}, "daily": [{"close": 100}]}, None)
    before = profile_store.get_entry("600519.SS")["tushare"]

    profile_store.upsert_tushare("600519.SS", {}, "连接失败")
    after = profile_store.get_entry("600519.SS")["tushare"]

    assert after["basic"] == before["basic"]
    assert after["daily"] == before["daily"]
    assert after["fetched_at"] == before["fetched_at"]
    assert after["fetch_error"] == "连接失败"
