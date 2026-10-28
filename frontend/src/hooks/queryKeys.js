/** @param {string[]} symbols */
export function watchlistProfileKey(symbols) {
  return (symbols || []).slice().sort().join(",");
}

export const qk = {
  /** @param {number|undefined|null} userId */
  watchlist: (userId) => ["watchlist", userId ?? "guest"],
  watchlistProfiles: (symKey, userId) => ["watchlist", "profiles", userId ?? "guest", symKey],
  priceContext: (symKey, userId) => ["watchlist", "price-context", userId ?? "guest", symKey],
  stockDetail: (symbol, userId) => ["watchlist", "stock-detail", userId ?? "guest", symbol],
  kline: (symbol, range, interval) => ["market", "kline", symbol, range, interval],
  transactionAgentViews: (symbol, asOf, userId) => ["transaction-agent", "views", userId ?? "guest", symbol || "", asOf || ""],
  tracking: (userId) => ["tracking", userId ?? "guest"],
  trackingTheses: (userId) => ["tracking", userId ?? "guest", "theses"],
  trackingThesis: (userId, id) => ["tracking", userId ?? "guest", "thesis", id || ""],
  trackingChanges: (userId) => ["tracking", userId ?? "guest", "changes"],
  xueqiuCompany: (symbol) => ["xueqiu", "company", symbol || ""],
  xueqiuComments: (symbol, userId) => ["xueqiu", "comments", userId ?? "guest", symbol || ""],
  newsArchive: (symbol, page, sort, scope = "analysis") => [
    "news",
    "archive",
    symbol || "",
    scope,
    page,
    sort,
  ],
};
