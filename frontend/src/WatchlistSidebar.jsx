import { useId, useMemo, useState } from "react";
import {
  AutoComplete,
  Button,
  Drawer,
  Input,
  Space,
  Spin,
  Tooltip,
  Upload,
  App as AntApp,
} from "antd";
import {
  DeleteOutlined,
  FileExcelOutlined,
  ImportOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import * as api from "./api.js";

function fmtPct(v) {
  if (v == null || String(v).trim() === "" || !Number.isFinite(Number(v))) return "—";
  const n = Number(v);
  const s = `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
  return s;
}

function pctTone(v) {
  if (v == null || String(v).trim() === "" || !Number.isFinite(Number(v))) return "";
  return Number(v) >= 0 ? "is-up" : "is-down";
}

function fmtPrice(value) {
  if (value == null || String(value).trim() === "" || !Number.isFinite(Number(value))) return "—";
  return Number(value).toFixed(2);
}

function shortCode(symbol) {
  const s = String(symbol || "");
  const m = s.match(/^(\d{6})\./);
  return m ? m[1] : s;
}

/**
 * 左侧自选股侧栏（同花顺风格：搜索即加、点选切换、批量粘贴/Excel）。
 */
export default function WatchlistSidebar({
  collapsed,
  onCollapsedChange,
  mobile = false,
  mobileOpen = false,
  onMobileClose,
  symbols,
  watchProfiles,
  priceContext,
  priceContextLoading,
  loadingList,
  savingList = false,
  disabled = false,
  stockIndexLoading,
  draft,
  setDraft,
  stockOptions,
  fetchStockOptions,
  selectedSymbol,
  onSelectSymbol,
  onAddFromSearch,
  onRemoveSymbol,
  onRefreshQuotes,
  onRebuildUniverse,
  reloadWatchlist,
}) {
  const { message } = AntApp.useApp();
  const [batchText, setBatchText] = useState("");
  const [batchOpen, setBatchOpen] = useState(false);
  const batchId = useId();
  const [importLoading, setImportLoading] = useState(false);
  const [xlsxLoading, setXlsxLoading] = useState(false);
  const editingDisabled = disabled || loadingList || savingList || importLoading || xlsxLoading;

  const rows = useMemo(
    () =>
      (symbols || []).map((sym) => {
        const profile = watchProfiles?.[sym] || {};
        const px = priceContext?.[sym] || {};
        const name = String(profile.name || px.name || "").trim() || shortCode(sym);
        const changePct = px.latest_change_pct ?? px.change_pct ?? px.pct_chg ?? px.change_1d_pct;
        const price = px.latest_price ?? px.last_close;
        return { sym, name, changePct, price };
      }),
    [symbols, watchProfiles, priceContext]
  );

  const runBatchImport = async () => {
    const t = (batchText || "").trim();
    if (!t) {
      message.warning("请粘贴股票代码");
      return;
    }
    setImportLoading(true);
    try {
      const r = await api.parseWatchlistImport(t, true);
      const n = Array.isArray(r.added) ? r.added.length : 0;
      message.success(n ? `已加入 ${n} 只` : "没有新代码");
      setBatchText("");
      if (reloadWatchlist) await reloadWatchlist();
    } catch (e) {
      message.error(api.getApiErrorMessage(e));
    } finally {
      setImportLoading(false);
    }
  };

  const beforeUploadXlsx = (file) => {
    const name = (file.name || "").toLowerCase();
    if (!name.endsWith(".xlsx") || file.size > 2 * 1024 * 1024) {
      message.warning("请上传 .xlsx（≤2MB）");
      return Upload.LIST_IGNORE;
    }
    void (async () => {
      setXlsxLoading(true);
      try {
        const r = await api.parseWatchlistImportXlsx(file, true);
        const n = Array.isArray(r.added) ? r.added.length : 0;
        message.success(n ? `已从 Excel 加入 ${n} 只` : "没有新代码");
        if (reloadWatchlist) await reloadWatchlist();
      } catch (e) {
        message.error(api.getApiErrorMessage(e));
      } finally {
        setXlsxLoading(false);
      }
    })();
    return false;
  };

  if (collapsed && !mobile) {
    return <aside className="market-watchlist market-watchlist--collapsed" aria-label="已收起的自选股">
      <Tooltip title="展开自选股" placement="right">
        <Button type="text" icon={<MenuUnfoldOutlined />} aria-label="展开自选股"
          onClick={() => onCollapsedChange(false)} />
      </Tooltip>
      <span className="market-watchlist__rail-title">自选</span>
      <span className="market-watchlist__count">{symbols.length}</span>
    </aside>;
  }

  const content = <aside className={`market-watchlist${mobile ? " market-watchlist--drawer" : ""}`} aria-label="自选股列表">
    {!mobile ? <div className="market-watchlist__heading">
      <h2>自选股 <span>{symbols.length}</span></h2>
      <Tooltip title="收起自选股">
        <Button type="text" size="small" icon={<MenuFoldOutlined />} aria-label="收起自选股"
          onClick={() => onCollapsedChange(true)} />
      </Tooltip>
    </div> : null}

    <div className="market-watchlist__search">
      <AutoComplete
        value={draft}
        disabled={editingDisabled}
        options={stockOptions}
        onSearch={fetchStockOptions}
        filterOption={false}
        onChange={(v) => setDraft(String(v || ""))}
        onSelect={(v) => onAddFromSearch(v, { clearDraft: true })}
        className="market-watchlist__autocomplete"
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            // Let AutoComplete submit its keyboard-highlighted option once.
            if (e.target.getAttribute("aria-activedescendant")) return;
            e.preventDefault();
            onAddFromSearch(draft, { clearDraft: true });
          }
        }}
      >
        <Input prefix={<SearchOutlined aria-hidden="true" />} placeholder="搜索名称或代码"
          aria-label="搜索股票名称或代码" allowClear />
      </AutoComplete>
      <Button icon={<PlusOutlined />} aria-label="添加股票" disabled={editingDisabled}
        loading={savingList || stockIndexLoading}
        onClick={() => onAddFromSearch(draft, { clearDraft: true })} />
    </div>

    <Spin spinning={loadingList || stockIndexLoading} wrapperClassName="market-watchlist__list-wrap">
      <div className="market-watchlist__list">
        {!rows.length ? <div className="market-watchlist__empty">
          <SearchOutlined aria-hidden="true" />
          <strong>还没有自选股</strong>
          <p>搜索名称或代码添加，也可批量导入。</p>
        </div> : rows.map((row) => {
          const active = row.sym === selectedSymbol;
          return <div key={row.sym} className={`market-watchlist__row${active ? " is-active" : ""}`}>
            <button type="button" className="market-watchlist__select" aria-pressed={active}
              aria-label={`${row.name} ${row.sym}`} onClick={() => onSelectSymbol(row.sym)}>
              <span className="market-watchlist__identity">
                <span className="market-watchlist__name" title={row.name}>{row.name}</span>
                <span className="market-watchlist__symbol">{row.sym}</span>
              </span>
              <span className={`market-watchlist__quote ${pctTone(row.changePct)}`}>
                <span className="market-watchlist__price">{fmtPrice(row.price)}</span>
                <span className="market-watchlist__change">{fmtPct(row.changePct)}</span>
              </span>
            </button>
            <Tooltip title={`移除 ${row.name}`}>
              <Button type="text" size="small" danger disabled={editingDisabled}
                aria-label={`移除 ${row.name}`} className="market-watchlist__remove" icon={<DeleteOutlined />}
                onClick={(e) => { e.stopPropagation(); onRemoveSymbol(row.sym); }} />
            </Tooltip>
          </div>;
        })}
      </div>
    </Spin>

    <footer className="market-watchlist__footer">
      <div className="market-watchlist__actions">
        <Button icon={<ReloadOutlined />} loading={priceContextLoading} disabled={disabled || !symbols.length}
          onClick={() => void onRefreshQuotes?.()}>刷新行情</Button>
        <Button icon={<ImportOutlined />} aria-expanded={batchOpen} aria-controls={batchId}
          onClick={() => setBatchOpen((value) => !value)}>批量导入</Button>
      </div>
      <div className="market-watchlist__import" id={batchId} hidden={!batchOpen}>
        <Input.TextArea value={batchText} onChange={(e) => setBatchText(e.target.value)}
          autoSize={{ minRows: 3, maxRows: 5 }} placeholder="600519 000001，或粘贴多行代码"
          aria-label="批量导入股票代码" disabled={editingDisabled} />
        <Space size={8} wrap>
          <Button size="small" type="primary" icon={<ImportOutlined />} loading={importLoading}
            disabled={editingDisabled} onClick={() => void runBatchImport()}>合并到自选</Button>
          <Upload accept=".xlsx" disabled={editingDisabled} showUploadList={false} beforeUpload={beforeUploadXlsx}>
            <Button size="small" icon={<FileExcelOutlined />} loading={xlsxLoading} disabled={editingDisabled}>Excel</Button>
          </Upload>
        </Space>
        <p>仅合并新代码 · Excel 最大 2 MB</p>
      </div>
      <Button className="market-watchlist__index" type="text" size="small" disabled={disabled}
        loading={stockIndexLoading} onClick={() => void onRebuildUniverse?.()}>更新股票检索库</Button>
    </footer>
  </aside>;

  return mobile ? <Drawer title={`自选股 · ${symbols.length}`} placement="left" open={mobileOpen}
    onClose={onMobileClose} width="min(320px, calc(100vw - 28px))" rootClassName="market-watchlist-drawer"
    styles={{ body: { padding: 0 } }} destroyOnHidden={false}>{content}</Drawer> : content;
}
