# 运维部署

## 本地开发

```bash
# 启动 API
uv run uvicorn panbridge.main:app --host 127.0.0.1 --port 8000 --reload

# 启动 Worker（另开终端）
uv run python -m panbridge.worker
```

## 生产部署方向

主要部署方向为 **systemd 原生服务**（后续 M3 阶段完善 NixOS 模块）。

### 环境变量

完整配置见 `.env.example`。

生产环境必须设置：

- `PANBRIDGE_API_KEY`：API 鉴权密钥
- `PANBRIDGE_SECRET_KEY`：凭据加密主密钥（强随机值）

## 数据与安全

- 数据库：SQLite WAL（默认），可切换 PostgreSQL（`PANBRIDGE_DATABASE_URL`）
- 凭据加密存储，主密钥外部注入
- 默认监听 `127.0.0.1`，不自动开放公网端口
- 遵守网盘服务条款：不实现验证码/风控/付费权限绕过，不做无限重试

## 监控

- 结构化日志（Python logging，后续接入结构化 JSON 日志）
- 任务状态可通过 `/v1/tasks/{id}` 轮询
- SSE / Webhook 推送在后续阶段实现
