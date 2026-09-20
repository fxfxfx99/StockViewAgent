import { useMemo, useState } from "react";
import {
  AutoComplete,
  Button,
  Collapse,
  Input,
  Space,
  Spin,
  Tooltip,
  Typography,
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
} from "@ant-design/icons";
import * as api from "./api.js";

const { Text } = Typography;

function fmtPct(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  const s = `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
  return s;
}

function pctTone(v) {
  if (v == null || Number.isNaN(Number(v))) return "";
  return Number(v) >= 0 ? "is-up" : "is-down";
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
  const [importLoading, setImportLoading] = useState(false);
  const [xlsxLoading, setXlsxLoading] = useState(false);
  const editingDisabled = disabled || loadingList || savingList || importLoading || xlsxLoading;

  const rows = useMemo(
    () =>
      (symbols || []).map((sym) => {
        const profile = watchProfiles?.[sym] || {};
        const px = priceContext?.[sym] || {};
        const name = String(profile.name || px.name || "").trim() || shortCode(sym);
        const changePct = px.change_pct ?? px.pct_chg ?? px.change_1d_pct;
        return { sym, name, changePct };
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
      message.success(n ? `已加入 ${n} 只` : "没有新代码（可能已在列表中）");
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

  if (collapsed) {
    return (
      <aside className="sva-watch-sider sva-watch-sider--collapsed">
        <Tooltip title="展开" placement="right">
          <Button
            type="text"
            className="sva-watch-sider__toggle"
            icon={<MenuUnfoldOutlined />}
            onClick={() => onCollapsedChange(false)}
          />
        </Tooltip>
        <div className="sva-watch-sider__rail-label">
          <span>股票列表</span>
          <span className="sva-watch-sider__rail-count">{symbols.length}</span>
        </div>
      </aside>
    );
  }

  return (
    <aside className="sva-watch-sider">
      <div className="sva-watch-sider__head">
        <span className="sva-watch-sider__title">
          股票列表
          <em>{symbols.length}</em>
        </span>
        <Tooltip title="收起">
          <Button type="text" size="small" icon={<MenuFoldOutlined />} onClick={() => onCollapsedChange(true)} />
        </Tooltip>
      </div>

      <div className="sva-watch-sider__search">
        <AutoComplete
          value={draft}
          disabled={editingDisabled}
          options={stockOptions}
          onSearch={fetchStockOptions}
          filterOption={false}
          onChange={(v) => setDraft(String(v || ""))}
          onSelect={(v) => onAddFromSearch(v, { clearDraft: true })}
          className="sva-watch-sider__ac"
          placeholder="代码 / 简称"
          allowClear
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              // 下拉选项存在键盘焦点时由 AutoComplete 的 onSelect 统一处理。
              if (e.target.getAttribute("aria-activedescendant")) return;
              e.preventDefault();
              onAddFromSearch(draft, { clearDraft: true });
            }
          }}
        />
        <Button
          type="primary"
          icon={<PlusOutlined />}
          aria-label="添加股票"
          disabled={editingDisabled}
          loading={savingList || stockIndexLoading}
          onClick={() => onAddFromSearch(draft, { clearDraft: true })}
        />
      </div>

      <div className="sva-watch-sider__actions">
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={priceContextLoading}
          disabled={disabled || !symbols.length}
          onClick={() => void onRefreshQuotes?.()}
        >
          行情
        </Button>
        <Button size="small" disabled={disabled} loading={stockIndexLoading} onClick={() => void onRebuildUniverse?.()}>
          检索库
        </Button>
      </div>

      <Spin spinning={loadingList || stockIndexLoading} className="sva-watch-sider__list-spin">
        <div className="sva-watch-sider__list">
          {!rows.length ? (
            <Text type="secondary" className="sva-watch-sider__empty">
              股票列表为空
            </Text>
          ) : (
            rows.map((row) => {
              const active = row.sym === selectedSymbol;
              return (
                <div
                  key={row.sym}
                  className={`sva-watch-sider__row${active ? " is-active" : ""}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSelectSymbol(row.sym)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelectSymbol(row.sym);
                    }
                  }}
                >
                  <div className="sva-watch-sider__row-main">
                    <span className="sva-watch-sider__name">{row.name}</span>
                    <span className="sva-watch-sider__code">{shortCode(row.sym)}</span>
                  </div>
                  <div className="sva-watch-sider__row-meta">
                    <span className={`sva-watch-sider__pct ${pctTone(row.changePct)}`}>
                      {fmtPct(row.changePct)}
                    </span>
                    <Button
                      type="text"
                      size="small"
                      danger
                      disabled={editingDisabled}
                      aria-label={`移除 ${row.name}`}
                      className="sva-watch-sider__del"
                      icon={<DeleteOutlined />}
                      onClick={(e) => {
                        e.stopPropagation();
                        onRemoveSymbol(row.sym);
                      }}
                    />
                  </div>
                </div>
              );
            })
          )}
        </div>
      </Spin>

      <Collapse
        ghost
        size="small"
        className="sva-watch-sider__batch"
        items={[
          {
            key: "batch",
            label: "批量添加",
            children: (
              <div>
                <Input.TextArea
                  value={batchText}
                  onChange={(e) => setBatchText(e.target.value)}
                  autoSize={{ minRows: 2, maxRows: 5 }}
                  placeholder="600519 000001 或粘贴多行"
                />
                <Space wrap size={6} style={{ marginTop: 8 }}>
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    icon={<ImportOutlined />}
                    loading={importLoading}
                    disabled={editingDisabled}
                    onClick={() => void runBatchImport()}
                  >
                    合并
                  </Button>
                  <Upload accept=".xlsx" disabled={editingDisabled} showUploadList={false} beforeUpload={beforeUploadXlsx}>
                    <Button size="small" icon={<FileExcelOutlined />} loading={xlsxLoading}>
                      Excel
                    </Button>
                  </Upload>
                </Space>
              </div>
            ),
          },
        ]}
      />
    </aside>
  );
}
