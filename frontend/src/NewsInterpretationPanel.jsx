import { lazy, Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Pagination,
  Segmented,
  Select,
  Space,
  Spin,
  Typography,
  App as AntApp,
} from "antd";
import * as api from "./api.js";
import { newsSyncNotice } from "./newsQueueViewModel.js";
import { qk } from "./hooks/queryKeys.js";
import { useNewsActions } from "./hooks/useNewsTracking.js";
import NewsImpactCard from "./components/NewsImpactCard.jsx";
import { useAuth } from "./AuthContext.jsx";

const { Text } = Typography;
const PAGE_SIZE = 8;
const EarningsReportsPanel = lazy(() => import("./EarningsReportsPanel.jsx"));

export default function NewsInterpretationPanel({ symbol, displayName }) {
  const { user } = useAuth();
  const [section, setSection] = useState("news");
  if (!symbol) return null;
  return <div><Segmented aria-label="新闻与财报内容" value={section} onChange={setSection} options={[{ value: "news", label: "新闻" }, { value: "earnings", label: "财报季" }]} style={{ marginBottom: 16 }} />
    {section === "news" ? <NewsContent key={`${user?.id}:${symbol}`} symbol={symbol} /> : <Card size="small" variant="borderless" className="ex-section-card sva-panel sva-news-panel"><Suspense fallback={<div style={{ padding: 32, textAlign: "center" }}><Spin aria-label="加载财报季" /></div>}><EarningsReportsPanel symbol={symbol} displayName={displayName} /></Suspense></Card>}
  </div>;
}

function NewsContent({ symbol }) {
  const { message } = AntApp.useApp();
  const [scope, setScope] = useState("analysis");
  const [sort, setSort] = useState("importance");
  const [page, setPage] = useState(1);
  const news = useQuery({
    queryKey: qk.newsArchive(symbol, page, sort, scope),
    queryFn: () =>
      api.getNewsArchive({
        symbol,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
        sort,
        scope,
        ...(scope === "pending" ? { lookback_days: 30 } : {}),
      }),
    enabled: Boolean(symbol),
  });
  const { sync, analyze } = useNewsActions();
  const busy = sync.isPending || analyze.isPending;

  const run = async (kind) => {
    try {
      const result =
        kind === "sync"
          ? await sync.mutateAsync(symbol ? [symbol] : null)
          : await analyze.mutateAsync(symbol);
      if (result?.ok === false) {
        message.error(result.detail || "分析失败");
        return;
      }
      if ((result?.analyzed || 0) === 0 && kind === "analyze") {
        message.info(result?.message || "暂无未解读新闻");
      } else if (kind === "analyze") {
        message.success(`已解读 ${result.analyzed} 条`);
      } else {
        message.info(newsSyncNotice(result));
      }
    } catch (error) {
      message.error(api.getApiErrorMessage(error));
    }
  };

  if (!symbol) return null;

  return (
    <Card
      size="small"
      variant="borderless"
      className="ex-section-card sva-panel sva-news-panel"
      extra={
        <Space wrap size={6}>
          <Button
            size="small"
            disabled={busy}
            loading={analyze.isPending}
            onClick={() => void run("analyze")}
            title="每批解读约需数分钟"
          >
            解读（{api.NEWS_ANALYSIS_BATCH_SIZE} 条/批）
          </Button>
          <Button
            size="small"
            type="primary"
            ghost
            disabled={busy}
            loading={sync.isPending}
            onClick={() => void run("sync")}
          >
            采集新闻
          </Button>
          <Button size="small" loading={news.isFetching} onClick={() => news.refetch()}>读取归档</Button>
        </Space>
      }
    >
      {sync.data ? <Alert type="info" showIcon message={newsSyncNotice(sync.data)} style={{ marginBottom: 12 }} /> : null}
      <div className="sva-news-toolbar">
        <div className="sva-news-toolbar__left">
          <Segmented
            size="small"
            value={scope}
            options={[
              { value: "analysis", label: "相关解读" },
              { value: "pending", label: "待解读" },
            ]}
            onChange={(value) => {
              setScope(value);
              setPage(1);
            }}
          />
          <Text type="secondary">
            {scope === "pending" ? "近30天 · " : ""}
            {news.data?.total || 0} 条
          </Text>
        </div>
        <Select
          size="small"
          value={sort}
          onChange={(value) => {
            setSort(value);
            setPage(1);
          }}
          options={[
            {
              value: "importance",
              label: scope === "pending" ? "重点事项优先" : "相关性 / 影响优先",
            },
            { value: "latest", label: "发布时间优先" },
          ]}
          className="sva-news-sort"
        />
      </div>

      {news.error ? (
        <Alert
          type="error"
          showIcon
          message={api.getApiErrorMessage(news.error)}
          action={<Button size="small" onClick={() => void news.refetch()}>重试</Button>}
        />
      ) : null}

      <Spin spinning={news.isFetching || busy}>
        <div className="news-stack">
          {news.data?.items?.length ? (
            news.data.items.map((item) => <NewsImpactCard key={item.id} item={item} symbol={symbol} />)
          ) : !news.isPending ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                scope === "pending"
                  ? "近 30 天暂无待解读新闻。"
                  : "暂无解读，可查看「待解读」。"
              }
            />
          ) : null}
        </div>
      </Spin>

      {(news.data?.total || 0) > PAGE_SIZE ? (
        <Pagination
          size="small"
          current={page}
          pageSize={PAGE_SIZE}
          total={news.data.total}
          onChange={setPage}
          showSizeChanger={false}
          className="sva-news-pager"
        />
      ) : null}
    </Card>
  );
}
