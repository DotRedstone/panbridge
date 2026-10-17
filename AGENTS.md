# AGENTS.md — PanBridge 项目规范

本文件为后续 AI 编程维护建立长期规范。所有参与本仓库开发的 AI 代理应先阅读本文。

## 项目定位

PanBridge 是一个多网盘自动转存与下载后端服务，通过 HTTP API 供外部程序（如未来的 AstrBot 插件）调用。

- 纯后端，无 Web 前端、无聊天机器人代码、无媒体刮削/自动追剧业务。
- 第一版优先夸克网盘，其他平台逐步扩展。

## 工程原则

1. **模块原子化、职责单一**：一个模块只做一件事，边界清晰。
2. **入口只组装**：`main.py` 等入口文件只负责初始化与装配，不堆积业务逻辑。
3. **依赖方向明确**：`domain` 不依赖框架；`application` 依赖 `domain`；`providers`/`downloads`/`persistence` 实现接口；`api` 依赖 `application`。禁止反向依赖。
4. **局部修复优先**：单个网盘问题在对应 Adapter 内解决，不写全局 workaround。
5. **不过度设计**：不为了设计模式而设计，不无意义拆文件。
6. **复杂模块配 README**：说明职责、入口和验证方式。

## 目录结构

```
src/panbridge/
├── main.py           # FastAPI 入口，只组装
├── api/              # HTTP API、鉴权、请求/响应模型
├── domain/           # 领域对象、枚举、错误（无框架依赖）
├── application/      # 业务用例编排
├── providers/        # 网盘适配器（base/registry + 各平台目录）
├── downloads/        # 下载接口与实现
├── persistence/      # SQLAlchemy 模型与仓库
├── workers/          # 后台任务执行
└── infrastructure/   # 配置、加密等基础设施
```

## 代码风格

- Python 采用标准文档字符串与类型注解，符合 Python 习惯。
- 使用 `ruff` 与 `pytest`，提交前确保通过。
- 行宽 88，Python 3.12+。

## Git 规范

- 采用 [Conventional Commits](https://www.conventionalcommits.org/)，英文 type/scope + 中文描述。
  - 示例：`feat(providers): 新增夸克网盘适配器`
- 一次提交只做一类改动，不混合无关重构、功能与文档整理。
- 所有改动可追踪、可验证、尽量可回滚。

## 安全红线

- **禁止提交**账号凭据、Cookie、Token、API Key、运行数据。
- 外部文本（分享链接）一律视为不可信输入。
- API 响应不得泄漏上游 Cookie、敏感请求头、Token 或 traceback。
- 默认监听 `127.0.0.1`，不自动开放公网端口。

## 提交前检查

- `ruff check src tests`
- `pytest`
- 检查 `git status --short`，不破坏已有改动。
- 不擅自提交 commit、部署线上、或修改外部仓库（除非用户明确要求）。

## 开发流程

1. 先检查仓库状态。
2. 明确本次改动范围，避免范围蔓延。
3. 实现 + 测试 + 文档同步更新。
4. 结束报告：修改文件、测试结果、剩余问题与风险。
