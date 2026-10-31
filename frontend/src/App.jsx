import { useCallback, useEffect, useMemo, useRef, useState, Suspense } from "react";
import { useAuth } from "./AuthContext.jsx";
import { Link } from "react-router-dom";
import dayjs from "dayjs";
import {
  Layout,
  Card,
  Alert,
  App as AntApp,
  Spin,
  DatePicker,
  Button,
  Tabs,
} from "antd";
import { DownloadOutlined, MenuUnfoldOutlined, ReloadOutlined } from "@ant-design/icons";
import * as api from "./api";
import { qk, watchlistProfileKey } from "./hooks/queryKeys.js";
import { useAppUrlState } from "./hooks/useAppUrlState.js";
import { useWatchlistServerState } from "./hooks/useWatchlistServerState.js";
import * as LazyPanels from "./lazyPanels.jsx";
import WatchlistProfilePanel from "./WatchlistProfilePanel.jsx";
import WatchlistSidebar from "./WatchlistSidebar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import AppToolbar from "./AppToolbar.jsx";
import "./MarketWorkspace.css";
import { useIsFetching, useQueryClient } from "@tanstack/react-query";
const { Content } = Layout;

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
  const { user, loading: bootLoading, error: authError, refreshMe, authRequired, logout } = useAuth();
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
  const [mobileWatchlistOpen, setMobileWatchlistOpen] = useState(false);
  const [isCompact, setIsCompact] = useState(() => window.matchMedia("(max-width: 900px)").matches);
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

  const focusSymbol = chartSymbol ?? symbols[0] ?? "";
  const klineBusy = useIsFetching({ queryKey: qk.kline(focusSymbol, KLINE_RANGE, klineInterval) }) > 0;
  const focusSecurityLabel = useMemo(
    () => securityLabel(focusSymbol, watchProfiles[focusSymbol]),
    [focusSymbol, watchProfiles]
  );
  const focusName = watchProfiles[focusSymbol]?.name || focusSymbol || "选择一只股票";
  const focusMarket = { SS: "沪市 A 股", SZ: "深市 A 股", BJ: "北交所 A 股" }[focusSymbol.split(".")[1]] || "A 股";

  useEffect(() => {
    const media = window.matchMedia("(max-width: 900px)");
    const update = () => {
      setIsCompact(media.matches);
      if (!media.matches) setMobileWatchlistOpen(false);
    };
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

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
        setMobileWatchlistOpen(false);
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
      message.warning("请先添加自选股");
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
        message.warning("部分资料刷新失败，显示已有资料");
      }
      else message.success("公司信息已刷新");
    } catch (e) {
      message.error(api.getApiErrorMessage(e));
    } finally {
      setProfilesRefreshing(false);
    }
  };

  const panels = [
    { key: "current-research", label: "当前研究", children: <PanelSuspense><LazyPanels.StockResearchContextLazy key={`${user?.id}:${focusSymbol}`} symbol={focusSymbol} /></PanelSuspense> },
    { key: "financial", label: "财务图表", children: <PanelSuspense><LazyPanels.StockFinancialPanelLazy key={`${user?.id}:${focusSymbol}`} symbol={focusSymbol} /></PanelSuspense> },
    {
      key: "decision",
      label: "决策分析",
      children: <PanelSuspense>
        <LazyPanels.TransactionAgentPanelLazy
          key={`${focusSymbol}:${asOf || ""}`}
          symbol={focusSymbol}
          displayName={focusSecurityLabel}
          asOf={asOf}
          headerControls={<label className="market-analysis-date">
            <span>分析日期</span>
            <DatePicker
              size="small"
              variant="borderless"
              value={asOf ? dayjs(asOf) : null}
              onChange={(d) => setAsOf(d ? d.format("YYYY-MM-DD") : null)}
              allowClear
              placeholder="最新"
              aria-label="决策分析日期"
              disabledDate={(current) => current && current > dayjs().endOf("day")}
            />
          </label>}
        />
      </PanelSuspense>,
    },
    {
      key: "news",
      label: "新闻与财报",
      children: <PanelSuspense>
        <LazyPanels.NewsInterpretationPanelLazy key={focusSymbol} symbol={focusSymbol} displayName={focusSecurityLabel} />
      </PanelSuspense>,
    },
    {
      key: "discussion",
      label: "市场讨论",
      children: <PanelSuspense>
        <LazyPanels.XueqiuCommentsPanelLazy key={`${user?.id}:${focusSymbol}`} symbol={focusSymbol} displayName={focusSecurityLabel} />
      </PanelSuspense>,
    },
    {
      key: "company",
      label: "公司资料",
      children: <Card
        size="small"
        variant="borderless"
        className="ex-section-card sva-panel"
        extra={<Button type="text" size="small" icon={<ReloadOutlined />}
          loading={profilesRefreshing || profilesLoading}
          onClick={() => void refreshWatchProfilesAll()}>刷新</Button>}
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
      </Card>,
    },
  ];

  return (
    <Layout className="sva-layout market-page">
      <AppToolbar
        active="market"
        settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />}
        user={user}
        authRequired={authRequired}
        onLogout={logout}
        leadingAction={isCompact ? <Button type="text" icon={<MenuUnfoldOutlined />}
          aria-label="打开自选股" aria-expanded={mobileWatchlistOpen}
          onClick={() => setMobileWatchlistOpen(true)} /> : null}
      />

      {bootLoading ? (
        <div className="sva-boot"><Spin size="large" aria-label="加载工作区" /></div>
      ) : (
        <div className={`market-shell${siderCollapsed ? " is-sidebar-collapsed" : ""}`}>
          <WatchlistSidebar
            collapsed={siderCollapsed}
            onCollapsedChange={setCollapsed}
            mobile={isCompact}
            mobileOpen={mobileWatchlistOpen}
            onMobileClose={() => setMobileWatchlistOpen(false)}
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
            onSelectSymbol={(sym) => { setChartSymbol(sym); setMobileWatchlistOpen(false); }}
            onAddFromSearch={addFromSearch}
            onRemoveSymbol={removeSymbol}
            onRefreshQuotes={() => invalidatePriceContext()}
            onRebuildUniverse={rebuildStockUniverse}
            reloadWatchlist={refetchWatchlist}
          />

          <Content className="market-main">
            {authError ? <Alert type="error" showIcon message={authError}
              action={<Button size="small" onClick={() => void refreshMe()}>重新连接</Button>}
              className="market-alert" /> : null}
            {error || watchlistError ? <Alert type="error" showIcon message={error || watchlistError}
              closable onClose={() => setError("")} className="market-alert" /> : null}

            <div className="market-workspace">
              <div className="market-security-toolbar">
                <div className="market-security-heading">
                  <h1 title={focusName}>{focusName}</h1>
                  <p>{focusSymbol ? <><span>{focusSymbol}</span><span aria-hidden="true"> · </span>{focusMarket}</> : "添加自选股以查看行情"}</p>
                </div>
                <div className="market-chart-actions">
                  {focusSymbol ? <Link className="ant-btn ant-btn-default" to={`/replay?symbol=${encodeURIComponent(focusSymbol)}`}>K线复盘</Link> : null}
                  <div className="market-periods" role="group" aria-label="K线周期">
                    {KLINE_INTERVALS.map((opt) => <button key={opt.value} type="button"
                      aria-pressed={klineInterval === opt.value}
                      className={klineInterval === opt.value ? "is-active" : ""}
                      onClick={() => setKlineInterval(opt.value)}>{opt.label}</button>)}
                  </div>
                  <Button type="text" icon={<ReloadOutlined />} loading={klineBusy}
                    disabled={!focusSymbol} onClick={() => klineRef.current?.refresh?.()}>刷新</Button>
                  <Button type="text" icon={<DownloadOutlined />} disabled={!focusSymbol}
                    onClick={() => klineRef.current?.download?.()} aria-label="导出 K 线 CSV">导出</Button>
                </div>
              </div>

              {focusSymbol ? <>
                <section className="market-chart-section" aria-label={`${focusSecurityLabel}行情`}>
                  <PanelSuspense>
                    <LazyPanels.StockKlinePanelLazy
                      ref={klineRef} symbol={focusSymbol} embedded hideChrome
                      chartHeight={isCompact ? 380 : 420}
                      displayName={focusSecurityLabel} range={KLINE_RANGE} interval={klineInterval} highlightDate={asOf}
                    />
                  </PanelSuspense>
                </section>
                <Tabs key={user?.id || "guest"} className="market-analysis-tabs" defaultActiveKey="current-research"
                  destroyOnHidden={false} items={panels} aria-label="个股研究内容" />
              </> : <div className="market-empty-workspace">
                <MenuUnfoldOutlined aria-hidden="true" />
                <h2>添加自选股</h2>
                <p>搜索名称或代码，加入自选。</p>
                {isCompact ? <Button type="primary" onClick={() => setMobileWatchlistOpen(true)}>添加自选股</Button> : null}
              </div>}
            </div>
          </Content>
        </div>
      )}
    </Layout>
  );
}
