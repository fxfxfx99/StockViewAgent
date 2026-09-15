export const invalidateNewsData = (client) =>
  Promise.all([
    client.invalidateQueries({ queryKey: ["news"] }),
    client.invalidateQueries({ queryKey: ["watchlist", "profiles"] }),
  ]);
