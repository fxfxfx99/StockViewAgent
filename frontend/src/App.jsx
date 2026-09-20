import { useCallback, useEffect, useMemo, useRef, useState, Suspense } from "react";
import { useAuth } from "./AuthContext.jsx";
import dayjs from "dayjs";
import {
  Layout,
  Typography,
  Card,
  Space,
  Alert,
  App as AntApp,
  Spin,
  DatePicker,
  Button,
} from "antd";
import * as api from "./api";
import { qk, watchlistProfileKey } from "./hooks/queryKeys.js";
import { useAppUrlState } from "./hooks/useAppUrlState.js";
import { useWatchlistServerState } from "./hooks/useWatchlistServerState.js";
import * as LazyPanels from "./lazyPanels.jsx";
import WatchlistProfilePanel from "./WatchlistProfilePanel.jsx";
import WatchlistSidebar from "./WatchlistSidebar.jsx";
import { ConfigConsoleButton, SetupOnboardingBanner } from "./ConfigConsole.jsx";
import { useSetupStatus } from "./hooks/useSetupStatus.js";
import ThemeModeToggle from "./ThemeModeToggle.jsx";
import { useIsFetching, useQueryClient } from "@tanstack/react-query";
const { Header, Content } = Layout;
const { Text } = Typography;

const SIDEBAR_KEY = "sva_watch_sider_collapsed";
const KLINE_INTERVALS = [
  { value: "1d", label: "日K" },
  { value: "1wk", label: "周K" },
  { value: "1mo", label: "月K" },
];
const KLINE_RANGE = "1y";

function PanelSuspense({ children }) {
  return (
    <Suspense
      fallback={
        <div className="sva-panel-skeleton">
          <Spin size="large" aria-label="加载模块" />
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

function securityLabel(symbol, profile) {
  const sym = String(symbol || "").trim().toUpperCase();
  const name = String(profile?.name || "").trim();
  return name && name !== sym ? `${name}（${sym}）` : sym;
}

function normalizeSymbolCandidate(raw) {
  const t = String(raw || "").trim().toUpperCase();
  if (/^\d{6}\.(SS|SZ|BJ)$/.test(t)) return t;
  const six = t.match(/^(\d{6})$/);
  if (six) {
    const code = six[1];
    if (code.startsWith("6") || code.startsWith("9")) return `${code}.SS`;
    if (code.startsWith("0") || code.startsWith("3")) return `${code}.SZ`;
    if (code.startsWith("8") || code.startsWith("4")) return `${code}.BJ`;
  }
  return null;
}

export default function App() {
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const { user, loading: bootLoading, error: authError, refreshMe } = useAuth();
  const { chartSymbol, setChartSymbol, asOf, setAsOf } = useAppUrlState();

  const {
    symbols,
    watchProfiles,
    priceContext,
    loadingList,
    savingList,
    profilesLoading,
    priceContextLoading,
    watchlistError,
    persistSymbols: persistWatchlistMutation,
    refetchWatchlist,
    applyProfilesUpdate: setWatchProfiles,
    invalidatePriceContext,
  } = useWatchlistServerState(user?.id);

  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [stockOptions, setStockOptions] = useState([]);
  const [stockIndexLoading, setStockIndexLoading] = useState(false);
  const [profilesRefreshing, setProfilesRefreshing] = useState(false);
  const [klineInterval, setKlineInterval] = useState("1d");
  const klineRef = useRef(null);
  const stockSearchTimer = useRef(null);
  const stockSearchVersion = useRef(0);
  const [siderCollapsed, setSiderCollapsed] = useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_KEY) === "1";
    } catch {
      return false;
    }
  });
  const { data: setupStatus } = useSetupStatus();

  const focusSymbol = chartSymbol ?? symbols[0] ?? "";
  const klineBusy = useIsFetching({ queryKey: qk.kline(focusSymbol, KLINE_RANGE, klineInterval) }) > 0;
  const focusSecurityLabel = useMemo(
    () => securityLabel(focusSymbol, watchProfiles[focusSymbol]),
    [focusSymbol, watchProfiles]
  );

  const setCollapsed = useCallback((v) => {
    setSiderCollapsed(v);
    try {
      localStorage.setItem(SIDEBAR_KEY, v ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, []);

  const persistSymbols = (next, onSaved) =>
    persistWatchlistMutation(next, { setProfilesRefreshing, setError, onSaved });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const m = await api.getStocksMeta();
        if (cancelled) return;
        const emCount = m.count || 0;
        const csvCount = m.universe_count || 0;
        const updatedAt = Math.max(m.updated_at || 0, m.universe_updated_at || 0);
        const staleSec = 12 * 3600;
        const isStale = !updatedAt || Date.now() / 1000 - updatedAt > staleSec;
        const needsBootstrap = emCount === 0 && csvCount === 0;
        if (!needsBootstrap && !isStale) return;

        if (needsBootstrap) setStockIndexLoading(true);
        try {
          if (m.universe_has_csv_source && csvCount === 0) {
            await api.rebuildStocksUniverseFromUpload();
          }
          if (emCount === 0 || isStale) {
            await api.refreshStocksList();
          }
        } catch {
          /* ignore */
        }
      } catch {
        /* ignore */
      } finally {
        if (!cancelled) setStockIndexLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const fetchStockOptions = useCallback((text) => {
    const version = ++stockSearchVersion.current;
    if (stockSearchTimer.current) clearTimeout(stockSearchTimer.current);
    const q = (text || "").trim();
    if (q.length < 1) {
      setStockOptions([]);
      return;
    }
    stockSearchTimer.current = setTimeout(async () => {
      try {
        const d = await api.searchStocks(q, 20);
        if (version !== stockSearchVersion.current) return;
        let items = d.items || [];
        if (!items.length && q.length >= 2) {
          try {
            await api.refreshStocksList();
            const retry = await api.searchStocks(q, 20);
            items = retry.items || [];
          } catch {
            /* ignore */
          }
        }
        if (version !== stockSearchVersion.current) return;
        setStockOptions(
          items.map((s) => ({
            value: s.symbol,
            label: (
              <div className="sva-stock-opt">
                <span className="sva-stock-opt__code">{s.code}</span>
                <span className="sva-stock-opt__name">{s.name}</span>
                <span className="sva-stock-opt__mkt">{s.symbol?.split(".")[1] || ""}</span>
              </div>
            ),
          }))
        );
      } catch {
        if (version === stockSearchVersion.current) setStockOptions([]);
      }
    }, 220);
  }, []);

  useEffect(() => {
    return () => {
      if (stockSearchTimer.current) clearTimeout(stockSearchTimer.current);
      stockSearchVersion.current += 1;
    };
  }, []);

  /** 同花顺式：搜索选中 / 回车 → 加入自选（若无）并切换到该股 */
  const addFromSearch = useCallback(
    async (raw, { clearDraft = false } = {}) => {
      let sym = normalizeSymbolCandidate(raw);
      if (!sym) {
        const q = String(raw || "").trim();
        if (!q) {
          message.warning("请输入代码或简称");
          return;
        }
        try {
          const d = await api.searchStocks(q, 8);
          const items = d.items || [];
          const exact = items.find(
            (s) =>
              String(s.code) === q ||
              String(s.symbol).toUpperCase() === q.toUpperCase() ||
              String(s.name) === q
          );
          sym = exact?.symbol || (items.length === 1 ? items[0].symbol : null);
        } catch {
          sym = null;
        }
      }
      if (!sym || !/^\d{6}\.(SS|SZ|BJ)$/.test(sym)) {
        message.warning("未匹配到股票");
        return;
      }
      const selectSaved = () => {
        if (clearDraft) {
          setDraft("");
          fetchStockOptions("");
        }
        setChartSymbol(sym);
      };
      if (!symbols.includes(sym)) {
        await persistSymbols(
          (previous) => previous.includes(sym) ? previous : [...previous, sym],
          () => {
            selectSaved();
            message.success(`已加入 ${sym}`);
          }
        );
      } else {
        selectSaved();
      }
    },
    [fetchStockOptions, message, persistSymbols, setChartSymbol, symbols]
  );

  const removeSymbol = useCallback(
    (sym) => {
      void persistSymbols(
        (previous) => previous.filter((s) => s !== sym),
        (saved) => {
          if (focusSymbol === sym) setChartSymbol(saved[0] || null);
        }
      );
    },
    [persistSymbols, focusSymbol, setChartSymbol]
  );

  const rebuildStockUniverse = useCallback(async () => {
    setStockIndexLoading(true);
    try {
      const meta = await api.getStocksMeta();
      if (meta.universe_has_csv_source) {
        const r = await api.rebuildStocksUniverseFromUpload();
        message.success(r.count > 0 ? `检索库已加载 ${r.count} 只` : r.detail || "已处理");
      } else {
        await api.refreshStocksList();
        message.success("已更新 A 股检索库");
      }
    } catch (err) {
      message.error(api.getApiErrorMessage(err));
    } finally {
      setStockIndexLoading(false);
    }
  }, [message]);

  const refreshWatchProfilesAll = async () => {
    if (!symbols.length) {
      message.warning("请先在左侧添加股票");
      return;
    }
    setProfilesRefreshing(true);
    try {
      const [profilesResult, companyResult] = await Promise.allSettled([
        api.refreshWatchlistProfiles(),
        focusSymbol ? api.getXueqiuCompany(focusSymbol, { force: true }) : Promise.resolve(null),
      ]);
      if (profilesResult.status === "fulfilled") {
        qc.setQueryData(qk.watchlistProfiles(watchlistProfileKey(symbols), user?.id), profilesResult.value.profiles || {});
        void qc.invalidateQueries({ queryKey: ["watchlist", "stock-detail"] });
      }
      if (companyResult.status === "fulfilled" && companyResult.value) {
        qc.setQueryData(qk.xueqiuCompany(focusSymbol), companyResult.value);
      }
      const failed = [profilesResult, companyResult].find((result) => result.status === "rejected");
      if (failed) throw failed.reason;
      if (Object.keys(profilesResult.value.errors || {}).length || ["partial", "stale", "unavailable"].includes(companyResult.value?.status)) {
        message.warning("部分公司资料刷新失败，已保留可用资料");
      }
      else message.success("公司信息已刷新");
    } catch (e) {
      message.error(api.getApiErrorMessage(e));
    } finally {
      setProfilesRefreshing(false);
    }
  };

  return (
    <Layout className="sva-layout">
      <Header className="sva-header">
        <span className="sva-brand">StockViewAgent</span>
        <Space size={4} className="sva-header-actions">
          <ThemeModeToggle />
          <ConfigConsoleButton isAdmin={user?.role === "admin"} />
        </Space>
      </Header>

      <SetupOnboardingBanner setupStatus={setupStatus} />

      {bootLoading ? (
        <div className="sva-boot">
          <Spin size="large" />
        </div>
      ) : (
        <div className="sva-shell">
          <WatchlistSidebar
            collapsed={siderCollapsed}
            onCollapsedChange={setCollapsed}
            symbols={symbols}
            watchProfiles={watchProfiles}
            priceContext={priceContext}
            priceContextLoading={priceContextLoading}
            loadingList={loadingList}
            savingList={savingList}
            disabled={!user}
            stockIndexLoading={stockIndexLoading}
            draft={draft}
            setDraft={setDraft}
            stockOptions={stockOptions}
            fetchStockOptions={fetchStockOptions}
            selectedSymbol={focusSymbol}
            onSelectSymbol={(sym) => setChartSymbol(sym)}
            onAddFromSearch={addFromSearch}
            onRemoveSymbol={removeSymbol}
            onRefreshQuotes={() => invalidatePriceContext()}
            onRebuildUniverse={rebuildStockUniverse}
            reloadWatchlist={refetchWatchlist}
          />

          <Content className="ex-app-content sva-main">
            {authError ? (
              <Alert
                type="error"
                showIcon
                message={authError}
                action={<Button size="small" onClick={() => void refreshMe()}>重新连接</Button>}
                className="sva-alert"
              />
            ) : null}
            {error || watchlistError ? (
              <Alert
                type="error"
                message={error || watchlistError}
                closable
                onClose={() => setError("")}
                className="sva-alert"
              />
            ) : null}

            <div className="sva-toolbar">
              <div className="sva-toolbar__left">
                <span className="sva-toolbar__sym">
                  {focusSymbol ? focusSecurityLabel : "—"}
                </span>
              </div>
              <div className="sva-toolbar__right">
                <label className="sva-field">
                  <span className="sva-field__label">节点</span>
                  <DatePicker
                    size="small"
                    variant="borderless"
                    value={asOf ? dayjs(asOf) : null}
                    onChange={(d) => setAsOf(d ? d.format("YYYY-MM-DD") : null)}
                    allowClear
                    placeholder="最新"
                    disabledDate={(current) => current && current > dayjs().endOf("day")}
                    className="sva-datepicker"
                  />
                </label>

                <div className="sva-seg" role="group" aria-label="K线周期">
                  {KLINE_INTERVALS.map((opt) => (
                    <button
                      key={opt.value}
                      type="button"
                      className={`sva-seg__item${klineInterval === opt.value ? " is-active" : ""}`}
                      onClick={() => setKlineInterval(opt.value)}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>

                <button
                  type="button"
                  className="sva-pill"
                  disabled={!focusSymbol || klineBusy}
                  onClick={() => klineRef.current?.refresh?.()}
                >
                  查询
                </button>
                <button
                  type="button"
                  className="sva-pill sva-pill--quiet"
                  disabled={!focusSymbol}
                  onClick={() => klineRef.current?.download?.()}
                >
                  下载 CSV
                </button>
              </div>
            </div>

            <Space direction="vertical" size={10} style={{ width: "100%" }}>
              <div className="sva-chart-block">
                {focusSymbol ? (
                  <PanelSuspense>
                    <LazyPanels.StockKlinePanelLazy
                      ref={klineRef}
                      symbol={focusSymbol}
                      embedded
                      hideChrome
                      chartHeight={456}
                      displayName={focusSecurityLabel}
                      range={KLINE_RANGE}
                      interval={klineInterval}
                    />
                  </PanelSuspense>
                ) : (
                  <Text type="secondary">从左侧选择股票</Text>
                )}
              </div>

              {focusSymbol ? (
                <PanelSuspense>
                  <LazyPanels.TransactionAgentPanelLazy
                    key={`${focusSymbol}:${asOf || ""}`}
                    symbol={focusSymbol}
                    displayName={focusSecurityLabel}
                    asOf={asOf}
                  />
                </PanelSuspense>
              ) : null}

              {focusSymbol ? (
                <PanelSuspense>
                  <LazyPanels.NewsInterpretationPanelLazy
                    key={focusSymbol}
                    symbol={focusSymbol}
                    displayName={focusSecurityLabel}
                  />
                </PanelSuspense>
              ) : null}

              {focusSymbol ? (
                <Card
                  size="small"
                  title="公司信息"
                  variant="borderless"
                  className="ex-section-card sva-panel"
                  extra={
                    <button
                      type="button"
                      className="sva-pill sva-pill--quiet"
                      disabled={profilesRefreshing || profilesLoading}
                      onClick={() => void refreshWatchProfilesAll()}
                    >
                      刷新
                    </button>
                  }
                >
                  <WatchlistProfilePanel
                    key={focusSymbol}
                    symbols={symbols}
                    profiles={watchProfiles}
                    loading={profilesLoading}
                    refreshing={profilesRefreshing}
                    onRefreshAll={refreshWatchProfilesAll}
                    onProfilesUpdate={setWatchProfiles}
                    singleSymbol={focusSymbol}
                    embedded
                  />
                </Card>
              ) : null}

              <p className="sva-footnote">仅供研究参考，不构成投资建议。</p>
            </Space>
          </Content>
        </div>
      )}
    </Layout>
  );
}
