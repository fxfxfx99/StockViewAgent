# 公网试用

在项目根目录执行：

```bash
./scripts/public-trial.sh start
./scripts/public-trial.sh status
./scripts/public-trial.sh stop
```

`start` 构建生产前端、启动独立后端和 Cloudflare Quick Tunnel，并在本机健康检查通过后显示 `https://….trycloudflare.com` 试用地址。打开该地址可以注册普通账户。大模型功能需要每位试用者在配置台填写自己的 API Key；公网试用仅支持内置 HTTPS 提供商地址。

首次使用请先执行 `./scripts/setup.sh`，准备 Python 虚拟环境和前端依赖。启动器从 `PATH` 或 Codex 的本机 Node.js 缓存查找 Node.js；缓存只提供 Node.js 而没有 npm 时，直接运行已安装的 Vite 完成同样的生产构建。Cloudflared 优先使用 `.public-trial/bin/cloudflared`，否则查找 `PATH`；macOS 可用 `brew install cloudflared` 安装。参考 [Cloudflare Quick Tunnels 文档](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)。

试用代码和数据位于 `.public-trial/runtime/`，不会复制开发环境的 `.env`、运行数据或上传文件。首次启动生成独立 JWT 密钥以及随机 `admin`、`user` 密码，保存在仅本机所有者可读的 `.public-trial/admin-credentials.json`（权限 `600`），启动器不会显示密码。试用者应自行注册账户。停止或重新启动会保留试用账户及数据；重新启动会更新代码、构建前端并生成新的公网地址。

后端仅监听 `127.0.0.1:8088`，使用单进程生产服务；端口已占用时启动器报错，不会终止其他服务。公开入口仅开放前端需要的接口，不提供 API 文档、任意上传或量化接口。定时任务、启动数据预热和公司资料自动刷新在试用副本中关闭。

macOS 启动器读取系统 HTTP/HTTPS 代理设置，仅提供给隧道进程；后端和前端构建不会继承开发环境密钥或代理配置。可用时，`caffeinate` 随后台管理进程运行以防空闲睡眠，停止试用后一起退出。

运行状态写入 `.public-trial/state.json`，日志分别在 `build.log`、`supervisor.log`、`tunnel.log` 和 `server.log`。`stop` 只终止状态文件中记录且进程身份仍匹配的试用进程。

这是依赖本机在线的临时试用入口：关闭电脑、休眠或断网会中断服务，Quick Tunnel 地址可能在重启后变化。本机健康检查通过只说明后端就绪，分享前仍需从公网打开链接，验证注册、登录与页面加载。Cloudflare Quick Tunnel 适合测试，没有可用性保证；长期发布请迁移到持续在线的服务器及固定域名。
