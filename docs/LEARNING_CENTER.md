# 学习入门

从主导航进入 **学习入门**（`/learn`）。默认显示零基础路径，可随时切换；不按交易年限或持仓情况自动判断基础。

| 路径 | 适合谁 | 学习顺序 |
|---|---|---|
| 零基础 | 还不熟悉股票、基金、收益和亏损风险，无需编程 | 梳理收支 → 认识品种和风险 → 选一本书阅读 → 完成一次有原文出处的观察 |
| 有基础 | 理解常见投资品种与基本风险，希望练习独立研究 | 读财报 → 建研究档案 → 历史复盘；量化编程另作可选方向 |

每一步都有具体练习，可勾选完成或取消。选择和进度按账户保存在当前浏览器的 `sva_learning_v1:<userId>` 中，不跨设备同步；浏览器无法保存时，页面会提示。链接可带 `?level=beginner` 或 `?level=experienced` 指定路径，非法值回退到已保存选择。切换路径保留另一条路径的完成记录。

## 精选资料

核对日期：2026-11-02。以下是阅读入口和本站编写的学习介绍，不在项目中镜像书籍或第三方教程正文。

| 资料 | 分层与用途 | 来源与授权 |
|---|---|---|
| [Financial Freedom](https://github.com/codeman008/Financial_freedom/blob/main/README.md) | 零基础；从入门书单选一个主题，再做阅读笔记 | GitHub 公开书单，未发现明确开源许可证；所列作品保留各自版权，图书使用正版或图书馆渠道 |
| [上交所证券基础课程](https://edu.sse.com.cn/college/required/basicinfo/) | 零基础；从「入市第一课」认识证券市场及风险 | 官方免费投教内容，不标为开源课程 |
| [Actual Budget](https://actualbudget.org/docs/getting-started/envelope-budgeting/) | 零基础可选实践；用虚构金额理解预算分配，英文可借助翻译 | [应用源码](https://github.com/actualbudget/actual) MIT；可直接打开[官方演示](https://demo.actualbudget.org/)，无需自行部署 |
| [上交所年报专题](https://edu.sse.com.cn/college/required/annual/) | 有基础；对照正式年报记录业务、财务数字与页码，无需编程 | 官方免费投教内容 |
| [Datawhale Quant for Beginners](https://github.com/datawhalechina/quant-for-beginners) | 有基础、愿意学习 Python / Jupyter；认识收益率、波动与回撤 | MIT；量化入门不等于个人理财零基础 |
| [Microsoft Qlib](https://github.com/microsoft/qlib) | 编程进阶；已理解 Python、统计、机器学习与回测后学习研究流程 | MIT；数据需另行核验，不能从框架示例推导盈利承诺 |

选择资料时优先考虑基础概念准确、阅读顺序清楚和前置要求明确，再考虑 GitHub 开源与否。公开仓库不自动等于开放授权；资料中的观点、数据接口和交易规则需随上游变化复核。资料配置位于 `frontend/src/learningResources.js`，更新链接时同步检查对应路径中的资源锚点。

## 与工作台的衔接

- 零基础完成阅读后，从「关注个股」查看公司资料和财报原文，先写事实、问题与出处。
- 有基础用户可进入「研究」保存问题与证据，在「K 线复盘」记录当时可见信息下的判断，并通过「监控」跟踪后续条件。
- 学习页面本身不调用模型或启动数据采集；工作台中的研究、模型解读和监控继续使用各自的设置与操作入口。
