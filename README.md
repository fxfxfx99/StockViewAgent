# StockViewAgent

一个 A 股研究工具，把行情、不同策略的观点、新闻和公司资料放在同一页，方便跟踪关注的股票。

## 行情与股票列表

搜索股票、批量添加到列表，选中后查看 K 线、均线、成交量和行情指标，支持日 K、周 K、月 K 切换。

下方截图可点击放大，展示的是使用时的页面，不代表实时行情。

[![StockViewAgent 单股工作台：自选列表、K 线和成交量](docs/images/overview.png)](docs/images/overview.png)

## 多策略观点

价值投资、质量成长、均值回归、事件驱动等观点并列展示。每张卡片列出判断方向、观察周期、分析依据和风险提示，可以展开查看详细分析，也可以指定历史日期生成策略观点。

[![多策略观点：不同研究视角的观点与分析卡片](docs/images/strategies.png)](docs/images/strategies.png)

## 新闻解读

集中查看与个股相关的新闻和解读，按时间或重要性排序，并保留原文入口。可以更新新闻，也可以为待解读的内容补充分析。

## 雪球评论精选

从最近 200 条个股讨论中，由模型筛选有研究参考价值的内容，展示观点摘要、入选理由、风险提示和原文链接，最多保留 20 条。

支持手动更新最近讨论或昨日讨论。启用定时更新并保持服务运行后，每天北京时间 09:00 整理关注股票前一天的讨论。若雪球要求登录或验证，需要先完成后再更新。

*下图为演示数据，不是真实雪球评论或筛选结论。*

[![雪球评论精选：摘要、入选理由与风险提示，使用演示数据](docs/images/xueqiu-comments-demo.png)](docs/images/xueqiu-comments-demo.png)

## 公司资料

查看公司简介、所属行业、主营业务，以及近期公告和资讯。资料附有来源和更新时间，便于核对；更新失败时保留上次结果并提示。

## 开始使用

项目在本机运行，需要 **Python 3.11–3.13** 和 **Node.js 20+**。

```bash
git clone https://github.com/fxfxfx99/StockViewAgent.git
cd StockViewAgent
./start.sh
```

启动后打开 [本地页面](http://127.0.0.1:5175/)，按配置台提示设置模型和行情来源。详细安装步骤、Docker 使用及常见问题见 [使用说明](docs/LOCAL_SETUP.md)。

## 更多说明

- [数据来源](docs/DATA_SOURCES.md)
- [雪球讨论与定时更新](backend/docs/XUEQIU_OPTIONAL.md#雪球评论精选)
- [分享给他人试用](docs/PUBLIC_TRIAL.md)
- [参与开发](CONTRIBUTING.md) · [许可证](LICENSE)

本项目用于整理研究信息，观点和解读仅供参考，不构成投资建议。
