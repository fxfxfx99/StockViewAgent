const STATUS = {
  queued: ["等待执行", "default"], running: ["执行中", "processing"], retry: ["等待重试", "warning"],
  completed: ["已完成", "success"], failed: ["执行失败", "error"], paused: ["已暂停", "warning"],
  evaluated: ["已评估", "blue"], shadow_started: ["开始模拟观察", "processing"],
  observing: ["模拟观察中", "processing"], active: ["运行中", "processing"],
  promoted: ["已晋升", "success"], rolled_back: ["已回滚", "warning"],
  holdout_rejected: ["保留样本未通过", "warning"], rejected: ["未通过", "warning"],
  frozen: ["已冻结候选", "blue"], shadow: ["模拟观察中", "processing"],
  data_unavailable: ["数据待补充", "warning"], configuration_required: ["需要配置", "warning"],
  insufficient_history: ["历史不足", "warning"], budget_exhausted: ["预算已用尽", "warning"],
  retired: ["已停止观察", "default"], superseded: ["已被替换", "default"],
};

const REASONS = {
  insufficient_development_or_holdout_sessions: "开发期或保留段的历史交易日不足",
  at_least_three_walk_forward_windows_required: "尚不足以形成三个滚动验证窗口",
  universe_history_not_aligned: "股票池的历史覆盖不一致", no_history: "尚无历史数据",
  example_or_synthetic_data: "格式样例或合成数据不能作为晋升证据",
  synthetic_example_not_market_evidence: "当前为虚构格式样例，不是真实市场证据",
  snapshot_not_strict: "观察使用过不完整的数据快照",
  cash_dividend_net_payment_unverified: "股息税及实际派款日期尚未完整核验",
  availability_after_decision_time: "行情在当日决策之后才可得，不能倒填历史信号",
  daily_bar_available_before_close: "最终日线的可用时间早于收盘",
  source_time_before_bar_close: "日线来源时间早于收盘，尚不能证明历史可得性",
  raw_execution_prices_missing: "缺少原始成交价格", raw_execution_price_unverified: "原始成交价格尚未验证",
  source_unknown: "数据来源不明确", historical_availability_missing: "缺少历史信息可用时间",
  trading_calendar_missing: "缺少完整交易日历", instrument_history_missing: "缺少证券历史状态",
  instrument_history_incomplete: "证券历史状态覆盖不完整", corporate_actions_history_missing: "缺少公司行动覆盖证据",
  corporate_actions_unchecked: "公司行动尚未核验", corporate_action_coverage_unverified: "公司行动覆盖尚未验证",
  historical_evidence_coverage_missing: "历史证据覆盖不足", historical_trading_status_missing: "缺少历史停牌或交易状态",
  historical_price_limits_missing: "缺少历史涨跌停价格", historical_price_limits_inferred: "涨跌停价格仍使用推断值",
  volume_unit_unknown: "成交量单位不明确", volume_unit_unverified: "成交量单位尚未验证",
  offline_or_stale_cache: "正在使用离线或较旧数据", missing_session_bars: "存在缺失交易日",
  mixed_source_or_price_basis: "数据源或复权口径不一致", source_or_price_basis_branch_changed: "新数据源需要重新核验",
  unmodeled_corporate_action: "公司行动尚不能完整计入回测", historical_fee_schedule_requires_override: "需要历史费用标准",
  strict_snapshot: "数据证据未达到自动晋升要求", history: "历史长度不足", data_quality: "数据质量未通过",
  statistics_complete: "统计证据不完整", positive_net_return: "扣费后收益未达要求",
  excess_consistency: "跨窗口超额收益不够稳定", minimum_completed_trades: "完整交易样本不足",
  drawdown_limit: "最大回撤超出要求", double_cost_positive: "双倍成本下收益未通过",
  double_cost_drawdown: "双倍成本下回撤未通过", prefix_invariance: "前视偏差检查未通过",
  warmup_stability: "指标预热稳定性未通过", minimum_holdout_sessions: "保留样本交易日不足",
  minimum_forward_sessions: "模拟观察交易日不足", same_evaluation_sessions: "对照区间不一致",
  beats_incumbent_sharpe: "风险调整收益未超过现行策略", incumbent_drawdown_not_worse: "回撤尚未优于现行策略",
  beats_equal_weight_return: "收益未超过等权基准",
  beats_incumbent_return: "收益未超过现行策略", incumbent_drawdown_tolerance: "回撤比现行策略恶化超过 2 个百分点",
};

export const RUN_LABELS = { collect: "历史采集", research: "自主研究", github: "学习来源更新", paper: "模拟账本更新", evaluate: "策略验证", rollback: "策略回滚" };
export const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "retry"]);
export const statusInfo = (status) => STATUS[status] || [status || "尚无状态", "default"];
export const reasonText = (reason) => REASONS[reason] || (typeof reason === "string" ? reason : JSON.stringify(reason));
export const finiteNumber = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
export function displayNumber(value, digits = 2) {
  const number = finiteNumber(value);
  return number === null ? "—" : number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
export const displayPercent = (value) => finiteNumber(value) === null ? "—" : `${displayNumber(value)}%`;
export function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}
export function parseJsonObject(text) {
  let value;
  try { value = JSON.parse(text); } catch { throw new Error("JSON 格式有误，请检查引号、逗号及括号。"); }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("请填写一个 JSON 对象。");
  return value;
}
export const experimentSummary = (entity) => entity?.summary || entity?.payload?.report || {};
export const researchQueryKey = (userId, scope = "overview", id = "") => ["research", userId, scope, id];

const GATE_LABELS = {
  strict_snapshot: "完整历史数据证据", history: "历史长度与验证窗口", data_quality: "数据质量",
  statistics_complete: "统计证据完整", positive_net_return: "扣费后正收益", excess_consistency: "跨窗口超额收益稳定性",
  minimum_completed_trades: "完整交易样本数", drawdown_limit: "最大回撤限制", double_cost_positive: "双倍成本下正收益",
  double_cost_drawdown: "双倍成本下回撤限制", prefix_invariance: "前视偏差检查", warmup_stability: "指标预热稳定性",
  minimum_holdout_sessions: "保留样本长度", minimum_forward_sessions: "前向观察长度", same_evaluation_sessions: "对照区间一致",
  beats_incumbent_sharpe: "风险调整收益优于现行策略", incumbent_drawdown_not_worse: "回撤不劣于现行策略",
  beats_equal_weight_return: "收益超过等权基准",
  beats_incumbent_return: "收益超过现行策略", incumbent_drawdown_tolerance: "回撤恶化不超过 2 个百分点",
};
export const gateLabel = (key) => GATE_LABELS[key] || key;
