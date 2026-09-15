/** @param {string[]} symbols */
export function watchlistProfileKey(symbols) {
  return (symbols || []).slice().sort().join(",");
}

export const qk = {
  /** @param {number|undefined|null} userId */
  watchlist: (userId) => ["watchlist", userId ?? "guest"],
  watchlistProfiles: (symKey) => ["watchlist", "profiles", symKey],
  priceContext: (symKey) => ["watchlist", "price-context", symKey],
  kline: (symbol, range, interval) => ["market", "kline", symbol, range, interval],
  transactionAgentViews: (symbol, asOf) => ["transaction-agent", "views", symbol || "", asOf || ""],
  xueqiuCompany: (symbol) => ["xueqiu", "company", symbol || ""],
  newsArchive: (symbol, page, sort, scope = "analysis") => [
    "news",
    "archive",
    symbol || "",
    scope,
    page,
    sort,
  ],
};
