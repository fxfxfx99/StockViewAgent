# 参与贡献

感谢你对 StockViewAgent 的兴趣。

## 本地开发

1. 按 [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) 跑通 `./start.sh`。
2. 仓库内的开发约定见 [AGENTS.md](AGENTS.md)（产品定位、目录、前端状态管理、环境变量）。
3. 改 API 或行为时同步更新注释与相关文档；新增环境变量时改 `backend/.env.example`。

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
cd frontend && npm run build
```

## 提交前请确认

- 没有把 `backend/.env`、`frontend/.env`、`backend/data/` 或密钥加进提交
- 单页仍能按同一只股票查看 K 线、多策略观点、新闻解读
- 对外文案使用「股票列表」「市场研判」

欢迎通过 Issue / Pull Request 讨论功能与缺陷。分析结果不构成投资建议。
