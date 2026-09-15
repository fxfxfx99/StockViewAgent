"""Priority is event triage, not a relevance gate or sentiment prediction."""
import pytest

from app.services.news_priority import classify_priority


@pytest.mark.parametrize("title", [
    "同有科技:关于筹划重大资产重组的进展公告",
    "同有科技:关于终止重大资产重组事项的公告",
    "同有科技:重大资产重组方案被否决",
    "同有科技:重大资产重组未获批准",
    "同有科技:关于收购目标公司股权的公告",
    "同有科技:关于全资子公司转让合伙财产份额的公告",
    "同有科技:出售核心资产事项进展",
    "同有科技:公司实际控制人变更公告",
    "同有科技:控制权拟发生变更的提示性公告",
    "同有科技:关于签署重大合同的公告",
    "同有科技:重大合同取消，公司披露终止原因",
    "同有科技:关于收到项目中标通知书的公告",
    "同有科技:关于股东减持计划实施完成的公告",
    "同有科技:董事长增持股份计划",
    "同有科技:股份回购方案进展公告",
    "同有科技:重大诉讼进展及风险提示公告",
    "同有科技:关于收到仲裁裁决的公告",
    "同有科技:关于收到立案告知书的公告",
    "同有科技:关于收到行政处罚决定书的公告",
    "同有科技:向特定对象发行股票申请获受理",
    "同有科技:关于收到向特定对象发行股票注册批复的公告",
    "同有科技:定增方案获审核通过",
    "同有科技:发行股份购买资产申请被否决",
    "同有科技:可转债发行申请获受理",
    "同有科技:可转债提前赎回公告",
    "同有科技:关于控股股东非经营性资金占用的公告",
    "同有科技:关于为子公司提供对外担保的公告",
    "同有科技:公司债券违约公告",
    "同有科技:关于股票停牌的公告",
    "同有科技:复牌公告",
    "同有科技:公司股票可能被实施退市风险警示的公告",
    "同有科技:撤销退市风险警示公告",
    "无锡科技:重大诉讼进展公告",  # Issuer names containing 无 are not denials.
])
def test_specific_positive_or_negative_event_is_major(title):
    result = classify_priority({"title": title})
    assert result["level"] == "major" and result["rank"] == 2 and result["label"] == "重大事项"
    assert result["reason"]
    assert not {"sentiment", "impact_direction", "relevance_score"} & result.keys()


@pytest.mark.parametrize("title", [
    "同有科技:关于召开2026年第三次临时股东会的通知",
    "同有科技:股票交易异常波动公告，不存在应披露未披露重大事项",
    "同有科技:关于不涉及重大资产重组的说明",
    "同有科技:不存在重大诉讼的说明",
    "同有科技:公司无重大诉讼及仲裁",
    "公司重大诉讼及仲裁情况：无",
    "重大诉讼：不适用",
    "同有科技:不存在控制权变更事项",
    "同有科技:控股股东暂无减持计划",
    "同有科技:2026年半年度非经营性资金占用及其他关联资金往来情况汇总表",
    "同有科技:对外担保管理制度",
    "同有科技:增减持管理办法",
    "同有科技:公司章程涉及股份回购条款",
    "同有科技:提请股东大会授权董事会办理发行事项",
    "同有科技:可转债持有人会议规则",
    "同有科技:发行股票一般性授权",
    "回购股份政策解读与征求意见",
    "同有科技:关于重大事项的常规风险提示",
    "同有科技:客户接待活动公告",
    "公司可能面临重大诉讼风险",
])
def test_denials_routine_notices_and_templates_are_not_promoted(title):
    assert classify_priority({"title": title}) == {"level": "normal", "rank": 0, "label": "", "reason": ""}


@pytest.mark.parametrize("title", [
    "同有科技:2026年半年度报告摘要", "同有科技:2026年半年度报告",
    "同有科技:年度业绩预告", "同有科技:业绩快报修正公告", "同有科技:第三季度报告",
    "Apple quarterly results",
])
def test_financial_disclosures_are_second_priority(title):
    result = classify_priority({"title": title})
    assert result["level"] == "earnings" and result["rank"] == 1 and result["label"] == "财报业绩"


def test_real_body_event_can_promote_an_opaque_title():
    result = classify_priority({"title": "同有科技:关于有关事项的说明", "content":
                                "公司近日收到监管部门下发的立案告知书，后续将及时披露调查进展。"})
    assert result["level"] == "major" and "正文" in result["reason"]


def test_negative_progress_is_not_confused_with_a_denial():
    result = classify_priority({"title": "同有科技:有关情况的说明", "content":
                                "公司未能完成重大资产重组，董事会决定终止本次交易。"})
    assert result["level"] == "major"
    result = classify_priority({"title": "同有科技:关于收购目标公司股权暨不构成重大资产重组的公告"})
    assert result["level"] == "major"


def test_body_postposed_denial_is_not_an_actual_event():
    item = {"title": "公司有关情况说明", "content": "公司已经核查，重大诉讼及仲裁情况：无。"}
    assert classify_priority(item)["rank"] == 0


def test_dividend_adjustment_template_is_not_a_restructuring_or_buyback_event():
    # Distribution notices describe how hypothetical share-count changes would
    # affect a dividend. They do not announce the listed restructuring examples.
    item = {"title": "金发科技:关于2026年中期利润分配方案的公告", "content":
            "公司拟派发现金红利。如在权益分派登记日期间，因重大资产重组股份回购注销等导致股本变动，"
            "公司拟维持分红比例不变。如后续股本发生变化，公司将另行披露。"}
    assert classify_priority(item)["rank"] == 0
    item["content"] += "公司已决定终止重大资产重组，相关交易不再继续。"
    assert classify_priority(item)["rank"] == 2


@pytest.mark.parametrize("title,body", [
    ("同有科技:风险提示", "公司可能面临重大诉讼风险，投资者应注意风险。"),
    ("同有科技:风险提示", "如公司被提起重大诉讼，将依法履行披露义务。"),
    ("同有科技:风险提示", "若未来发生债务违约，公司将按规定披露。"),
    ("同有科技:经营制度", "公司应加强对外担保管理，预防诉讼风险。"),
    ("同有科技:2026年半年度报告", "公司已建立股份回购及增减持事项管理流程。"),
    ("同有科技:2026年半年度报告", "重大诉讼及仲裁事项：无。公司不存在相关事项。"),
    ("同有科技:关于召开股东会的通知", "公司拟审议重大资产重组相关授权议案。"),
])
def test_boilerplate_and_meeting_agendas_do_not_promote_news(title, body):
    assert classify_priority({"title": title, "content": body})["rank"] < 2


def test_retains_original_source_event_after_syndication_without_using_ai_text():
    result = classify_priority({"title": "同有科技重要事项", "source_articles": [
        {"title": "同有科技:重大合同终止公告", "summary": "公司决定终止合同。"}]})
    assert result["rank"] == 2
    assert classify_priority({"title": "同有科技经营消息", "analysis": {
        "reasoning": "公司可能被立案调查"}, "source_articles": "bad-format"})["rank"] == 0


def test_priority_never_mutates_source_data_or_infers_direction():
    item = {"title": "同有科技:减持计划完成公告", "related_codes": ["300302.SZ"]}
    before = dict(item)
    classify_priority(item)
    assert item == before
