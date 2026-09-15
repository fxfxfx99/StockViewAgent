from app.storage import universe_company_store


def test_merge_into_display_fills_empty():
    universe_company_store.upsert_symbol(
        "000001.SZ",
        {
            "name": "平安银行",
            "org_name": "平安银行股份有限公司",
            "main_business": "吸收公众存款",
            "industry": "银行",
            "intro": "简介",
        },
        None,
    )
    merged = {"name": "", "org_name": "", "main_business": "", "industry": "", "intro": ""}
    out = universe_company_store.merge_into_display(merged, "000001.SZ")
    assert out["org_name"] == "平安银行股份有限公司"
    assert out["industry"] == "银行"


def test_merge_does_not_override_nonempty():
    universe_company_store.upsert_symbol(
        "000002.SZ",
        {"name": "B", "org_name": "U", "main_business": "", "industry": "", "intro": ""},
        None,
    )
    merged = {"name": "手改", "org_name": "", "main_business": "", "industry": "", "intro": ""}
    out = universe_company_store.merge_into_display(merged, "000002.SZ")
    assert out["name"] == "手改"
    assert out["org_name"] == "U"
