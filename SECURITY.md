# 安全说明

- **不要**把 API Key、Cookie、`.env` 或 `backend/data/` 提交到 Git 或贴进公开 Issue。
- 默认 `AUTH_REQUIRED=false` 仅适合本机。暴露到公网时请设 `AUTH_REQUIRED=true`，并使用自定义 `AUTH_JWT_SECRET` 与强密码。
- 本软件输出不构成投资建议。

漏洞请通过仓库 Issue 私下说明复现步骤（不要附带密钥）。本地配置见 [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md)。
