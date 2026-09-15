from app.services import kline_learning_service as kls


def test_normalize_structured_analysis_has_stable_sections():
    out = kls.normalize_structured_analysis(
        {
            "market_overview": "震荡",
            "trend_judgment": "中性",
            "support_levels": "100 元",
            "resistance_levels": ["110 元"],
            "risk_warnings": [],
            "action_reference": ["放量突破后再观察"],
        }
    )
    assert out["support_levels"] == ["100 元"]
    assert out["resistance_levels"] == ["110 元"]
    assert out["risk_warnings"]
    assert out["disclaimer"] == "仅供分析参考，不构成投资建议。"


def test_strategy_prompts_are_migrated_for_old_prompt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(kls, "_root", lambda: tmp_path)
    (tmp_path / kls.PROMPT_NAME).write_text(
        '{"system":"old","user_template":"{symbol}","market_system":"market","market_user_template":"{index_key}"}',
        encoding="utf-8",
    )
    prompt = kls.get_prompt()
    assert prompt["system"] == "old"
    assert set(prompt["strategies"]) == set(kls.DEFAULT_STRATEGIES)
    assert prompt["strategies"]["risk"]["label"] == "风险提示"


def test_structured_markdown_keeps_non_advice_disclaimer():
    data = kls.normalize_structured_analysis({})
    text = kls.structured_analysis_markdown(data)
    assert "## 行情概况" in text
    assert "## 风险提示" in text
    assert text.endswith("仅供分析参考，不构成投资建议。")
