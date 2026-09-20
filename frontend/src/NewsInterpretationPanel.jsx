import { useState } from "react";
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
import { qk } from "./hooks/queryKeys.js";
import { useNewsActions } from "./hooks/useNewsTracking.js";
import NewsImpactCard from "./components/NewsImpactCard.jsx";

const { Text } = Typography;
const PAGE_SIZE = 8;

export default function NewsInterpretationPanel({ symbol, displayName }) {
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
        message.success(
          result?.merged_unique
            ? `已同步 ${result.merged_unique} 条新闻`
            : "新闻已更新"
        );
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
      title="相关新闻解读"
      extra={
        <Space wrap size={6}>
          <Button
            size="small"
            disabled={busy}
            loading={analyze.isPending}
            onClick={() => void run("analyze")}
          >
            补充分析（{api.NEWS_ANALYSIS_BATCH_SIZE}条/批）
          </Button>
          <Button
            size="small"
            type="primary"
            ghost
            disabled={busy}
            loading={sync.isPending}
            onClick={() => void run("sync")}
          >
            更新新闻
          </Button>
        </Space>
      }
    >
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
            {displayName ? `${displayName} · ` : ""}
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
              label: scope === "pending" ? "重点事项优先" : "相关 / 影响优先",
            },
            { value: "latest", label: "发布时间优先" },
          ]}
          className="sva-news-sort"
        />
      </div>

      <Text type="secondary" style={{ display: "block", marginBottom: 10 }}>
        每批最多解读 {api.NEWS_ANALYSIS_BATCH_SIZE} 条新闻，可能需要数分钟；完成后可继续补充。
      </Text>

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
            news.data.items.map((item) => <NewsImpactCard key={item.id} item={item} />)
          ) : !news.isPending ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                scope === "pending"
                  ? "近30天暂无待解读新闻。可先点「更新新闻」。"
                  : "暂无通过相关性筛选的解读。可切换「待解读」或先更新新闻。"
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
