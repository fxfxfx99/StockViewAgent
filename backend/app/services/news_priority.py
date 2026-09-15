"""Conservative processing priority, independent of relevance and price impact.

"Major" means inspect an explicit corporate event first. It neither determines
legal materiality nor asserts positive/negative investment impact. Ordinary news
still passes through the same matching and model-analysis admission rules.
"""
from __future__ import annotations

import re
from typing import Any

from app.schemas.news import clean_text


_RULES = [(re.compile(pattern), reason) for pattern, reason in (
    (r"重大资产重组|资产重组|重组(?:预案|方案|交易|事项)|"
     r"(?:收购|并购|吸收合并).{0,24}(?:股权|股份|资产|公司|业务)|"
     r"(?:股权|资产).{0,8}(?:收购|并购)|"
     r"(?:出售|转让|剥离|处置).{0,30}(?:重大资产|核心资产|全部资产|子公司|股权|合伙财产份额)|"
     r"(?:重大资产|核心资产|子公司|股权)(?:出售|转让|处置)", "重组、并购或资产交易进展"),
    (r"控制权.{0,8}(?:变更|转让)|(?:控股股东|实际控制人).{0,12}(?:变更|变动)|"
     r"(?:变更|变动).{0,12}(?:控股股东|实际控制人)|权益变动(?:报告书|提示|公告)", "控制权或重要股权变动"),
    (r"(?:重大|重要)(?:销售|采购|项目)?合同|重大(?:项目)?中标|"
     r"中标(?:公告|通知书|候选人|重大|项目)|(?:项目|工程|合同)(?:中标|中选)", "重大合同或中标事项"),
    (r"增持|减持|回购(?:股份|股票|注销|进展|计划|方案|完成|实施)|"
     r"(?:股份|股票).{0,8}回购", "增减持或股份回购进展"),
    (r"重大(?:诉讼|仲裁)|(?:诉讼|仲裁)(?:进展|裁决|判决|裁定|结果)|"
     r"(?:涉及|收到|提起|提请|新增|遭遇|被诉).{0,12}(?:诉讼|仲裁)|"
     r"立案(?:调查|告知书|通知书)|(?:被|遭|收到).{0,12}(?:立案|行政处罚|处罚决定)|"
     r"(?:重大|行政)处罚", "诉讼仲裁或监管调查处罚"),
    (r"(?:向特定对象|非公开|公开)发行(?:股票|股份).{0,20}(?:受理|批复|注册|审核|完成|终止|取消|否决)|"
     r"发行(?:股票|股份).{0,8}(?:购买资产|募集配套资金)|"
     r"(?:定向增发|定增|配股).{0,16}(?:获批|核准|批复|受理|注册|完成|终止|取消|否决|预案|方案)|"
     r"(?:发行|注册|提前赎回|强制赎回|停止转股).{0,8}可转债|"
     r"可转债.{0,14}(?:获批|核准|批复|受理|注册|上市|发行|完成|终止|取消|否决|提前赎回|强制赎回)",
     "股权融资或可转债实质进展"),
    (r"资金占用|违规担保|对外担保|担保(?:额度|代偿|违约)|"
     r"(?:债务|债券|借款|贷款|票据).{0,8}(?:逾期|违约)|"
     r"(?:逾期|违约).{0,8}(?:债务|债券|借款|贷款|票据)|"
     r"债务重组|破产(?:重整|清算|申请)|(?:申请|受理).{0,12}破产", "资金、担保或债务风险事项"),
    (r"退市(?:风险警示|风险|整理|决定|公告|提示)|终止上市|暂停上市|"
     r"(?:股票|证券|交易).{0,10}(?:停牌|复牌)|停复牌|(?:停牌|复牌)(?:公告|提示|进展)|"
     r"(?:实施|撤销).{0,6}其他风险警示", "停复牌或上市资格风险"),
)]
_EARNINGS = re.compile(r"(?:半年度|年度|季度)(?:报告|业绩|财务)|半年报|年报|季报|"
                       r"业绩(?:预告|快报|修正|更正|报告|说明会)|盈利预警|财务报告|财报|"
                       r"(?:earnings|interim|annual|quarterly)\s+(?:report|results)", re.I)
_MEETING_NOTICE = re.compile(r"(?:召开|举行).{0,30}(?:股东(?:大)?会|董事会|监事会).{0,15}(?:通知|提示)|"
                             r"(?:股东(?:大)?会|董事会|监事会).{0,12}(?:会议通知|召开通知)")
_TEMPLATE = re.compile(r"(?:管理|业务|工作|议事|信息披露)(?:制度|办法|规则)|"
                       r"公司章程|重大事项提示|风险因素|目录|法律法规|仅供参考|征求意见|政策解读|"
                       r"持有人会议规则|一般性授权|发行授权")
_DENIAL = re.compile(r"(?:不存在|未发生|未涉及|不涉及|不构成|未构成|没有|并无|暂无|无须|无需|不会).{0,14}$")
_NO_EVENT = re.compile(r"无(?:任何|新增|相关|其他|应披露(?:未披露)?)?$")
_FUTURE_DENIAL = re.compile(r"^(?:计划)?(?:不存在|不涉及|无变化|未发生|传闻不实|消息不实)")
_POST_DENIAL = re.compile(r"^(?:及(?:诉讼|仲裁))?(?:事项|情况|进展)?\s*[:：]?\s*"
                         r"(?:无(?:此类|相关)?(?:事项|情况)?|不适用|不存在|未发生)(?:[，,。；;\s]|$)")
_HYPOTHETICAL = re.compile(r"如果|倘若|假如|若(?:未来|发生|公司|出现|存在)|"
                           r"如(?:公司|发生|出现|存在|相关|未|不能|在|因|未来|后续)|"
                           r"可能(?:面临|发生|涉及|产生)|(?:预防|防范|避免|应对).{0,8}(?:风险|诉讼|仲裁|处罚)")
_SUBJECT = re.compile(r"公司|本集团|发行人|控股股东|实际控制人|董事|股东|子公司")
_ACTION = re.compile(r"拟|将|已|签订|签署|收到|完成|终止|取消|决定|审议通过|遭|被|发生|触及|提起|取得|中标")


def _major_reason(text: str, *, body: bool = False) -> str | None:
    if _TEMPLATE.search(text) or _HYPOTHETICAL.search(text):
        return None
    if body and not (_SUBJECT.search(text) and _ACTION.search(text)):
        return None
    for pattern, reason in _RULES:
        for match in pattern.finditer(text):
            before = re.split(r"[，,；;。！？]", text[:match.start()])[-1][-30:]
            after = text[match.end():match.end() + 22]
            if (_DENIAL.search(before) or _NO_EVENT.search(before)
                    or _FUTURE_DENIAL.search(after) or _POST_DENIAL.search(after)):
                continue
            # Interim/annual templates contain routine account tables and a
            # standard declaration, not evidence that occupation occurred.
            if match.group() == "资金占用" and re.search(r"及其他关联资金往来|情况汇总表|专项(?:报告|说明)", after):
                continue
            return reason
    return None


def classify_priority(item: dict[str, Any]) -> dict[str, Any]:
    """Return major/2, earnings/1 or normal/0; never bypass relevance gates."""
    sources = [item]
    source_articles = item.get("source_articles")
    if isinstance(source_articles, list):
        sources.extend(row for row in source_articles[:16] if isinstance(row, dict))
    prepared = []
    earnings = False
    for source in sources:
        title = clean_text(source.get("title"), 1000)
        meeting = bool(_MEETING_NOTICE.search(title))
        if not meeting and (reason := _major_reason(title)):
            return {"level": "major", "rank": 2, "label": "重大事项", "reason": reason + "，优先核查进展。"}
        financial = bool(_EARNINGS.search(title))
        earnings = earnings or financial
        prepared.append((source, meeting or financial or bool(_TEMPLATE.search(title))))
    for source, skip_body in prepared:
        if skip_body:
            continue
        # Summary may be an actual publisher excerpt when a long filing cannot
        # be read in full. Never use a historical AI interpretation as evidence.
        raw = source.get("content") or source.get("summary") or ""
        if not isinstance(raw, str):
            continue
        for sentence in re.split(r"[。！？；\n]", raw[:3500]):
            text = clean_text(sentence, 600)
            if reason := _major_reason(text, body=True):
                return {"level": "major", "rank": 2, "label": "重大事项", "reason": reason + "，正文披露了具体进展。"}
    if earnings:
        return {"level": "earnings", "rank": 1, "label": "财报业绩", "reason": "定期报告或业绩披露，优先核对经营变化。"}
    return {"level": "normal", "rank": 0, "label": "", "reason": ""}
