import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Empty, Select, Spin, Table } from "antd";
import { useAuth } from "./AuthContext.jsx";
import { financialSeries } from "./stockContextViewModel.js";
import * as api from "./api.js";
import "./StockResearchContext.css";

function FinancialChart({ series }) {
  const points = series.points;
  const values = points.map((point) => point.value);
  const min = Math.min(...values), max = Math.max(...values), span = max - min || Math.max(Math.abs(max) * .1, 1);
  const mapped = points.map((point, index) => ({ ...point, x: 76 + index / Math.max(points.length - 1, 1) * 560, y: 220 - (point.value - min) / span * 160 }));
  return <svg className="stock-financial-chart" viewBox="0 0 700 280" role="img" aria-label={`${series.field}原表数值趋势`}><line x1="76" y1="230" x2="640" y2="230" stroke="currentColor" opacity=".25" /><text x="8" y="60">{Number(max).toPrecision(5)}</text><text x="8" y="220">{Number(min).toPrecision(5)}</text><polyline fill="none" stroke="var(--sva-accent)" strokeWidth="2.5" points={mapped.map((point) => `${point.x},${point.y}`).join(" ")} />{mapped.map((point, index) => <g key={point.date}><circle cx={point.x} cy={point.y} r="4" fill="var(--sva-accent)"><title>{point.date}：{point.value} {series.unit || "单位未提供"}</title></circle>{index === 0 || index === mapped.length - 1 || mapped.length <= 5 ? <text x={point.x} y="258" textAnchor="middle">{point.date}</text> : null}</g>)}</svg>;
}
export default function StockFinancialPanel({ symbol }) {
  const { user } = useAuth();
  const [selected, setSelected] = useState(null);
  const query = useQuery({ queryKey: ["stock-financial-history", user?.id, symbol], queryFn: ({ signal }) => api.getStockFinancialHistory(symbol, { signal }), enabled: Boolean(symbol), retry: false });
  const series = useMemo(() => financialSeries(query.data?.items), [query.data]);
  const current = series.find((item) => item.key === selected) || series[0];
  return <div className="stock-financial-panel"><div className="stock-context-heading"><h2>财务原表趋势</h2><Button onClick={() => query.refetch()} loading={query.isFetching}>更新本地记录</Button></div><p className="stock-context-note">按报告类型、来源类别、同期和单位分组，保留原表字段与数值。</p>{query.error ? <Alert type="warning" message={api.getApiErrorMessage(query.error)} /> : null}{query.isPending ? <Spin /> : current ? <><Select aria-label="财务指标与统计口径" className="stock-financial-select" value={current.key} onChange={setSelected} options={series.map((item) => ({ value: item.key, label: `${item.field} · ${item.reportType} · ${item.period} · ${item.unit || "单位未提供"} · ${item.sourceType}` }))} /><p className="stock-context-note">{current.field} · {current.unit ? `单位：${current.unit}` : "单位未提供"} · {current.points.length}个报告期</p>{current.points.length > 1 ? <FinancialChart series={current} /> : <p className="stock-context-note">仅有 1 个报告期</p>}<Table rowKey="date" size="small" pagination={false} scroll={{ x: 550 }} dataSource={current.points} columns={[{ title: "报告期", dataIndex: "date" }, { title: "原表数值", dataIndex: "value", render: (value) => String(value) }, { title: "来源", dataIndex: "source", render: (value) => value || "未标注" }]} /></> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无财务数据，请先采集公司财务资料。" />}{query.data?.note ? <p className="stock-context-note">{query.data.note}</p> : null}</div>;
}
