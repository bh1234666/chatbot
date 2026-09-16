# Chatbot 后端

> Author: **bh1234666** · License: [MIT](./LICENSE)

群聊机器人后端:FastAPI + 多层记忆(hot/warm/cold/kb)+ LLM 编排 + 工具执行/委派 + OCR + 后台 "dream" 空闲整理子系统。

## 快速开始

需要 Python 3.12+。先建立并激活虚拟环境，然后安装依赖：

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux / macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
```

将 `.env.example` 复制为 `.env`，填入自己的 API 密钥和服务地址，再启动：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

健康检查 `GET /health`,Prometheus 指标 `GET /metrics`。

Windows 可使用 `start_backend.bat` 和 `start_agent.bat`；`start_no_gpu.bat`
关闭本地 GPU 功能。QQ 接入需单独安装 NapCat，并设置自己的 `QQ_BOT_NUM`。
模型看图和图片生成通过 `.env.example` 中的独立开关及 API 配置启用。

## 开源范围与发布

本仓库是根目录 Chatbot 后端及静态前端的 MIT 公开快照。
发布内容由 [`scripts/export_public_snapshot.py`](./scripts/export_public_snapshot.py)
按白名单导出；本地数据库、聊天记录、密钥、私人 QQ 标识、部署工作区及第三方运行时不随快照发布。
导出步骤、文件范围与外部组件说明见 [公开快照说明](./docs/public_snapshot.md)。

## 目录结构

```
app/
├── main.py             FastAPI 入口、迁移、生命周期(MinerU/dream 启停)
├── config.py           全局配置(pydantic-settings,env 覆盖)
├── api/                HTTP 路由(chat / memory / bot / archives / personas / ...)
├── core/               编排、上下文、权限、metrics、dream 子系统
│   └── dream/          后台空闲整理(信息量驱动的 maintenance 升级版)
├── llm/                LLM 客户端、chat 循环、工具(delegate/workspace/office/ocr/...)
├── memory/             分层记忆(hot/warm/cold/kb/archive/group_*)
├── db/                 连接池 + SQL 方言翻译
│   ├── pool.py         多连接 SQLite 池(WAL + 显式事务)
│   └── sql_translate.py  PG→SQLite 翻译器(纯 stdlib,带编译缓存)
└── schemas/            Pydantic 模型
```

## ⚠️ 重要运行约束:单 worker

全工程的**跨请求状态都保存在进程内存**中:幂等去重缓存(`api/chat.py`)、
helper completion ledger、各类 `asyncio.Lock`/`Semaphore`、`/metrics` 计数、
dream 状态。这些在多 worker / 多进程下**不共享**,会导致:

- 幂等去重失效(重复请求漏判);
- metrics 各 worker 各算各的;
- 进程内锁无法跨进程互斥。

因此当前架构**必须以单 worker 运行**(`--workers 1`)。`config.py` 已预留
`REDIS_URL` 但尚未接线;水平扩展需先把上述状态迁移到 Redis。

## 数据库

默认 SQLite(`DATABASE_URL=sqlite:///chatbot.db`)。代码内 SQL 用 PostgreSQL 语法
书写,运行时由 `app/db/sql_translate.py` 翻译为 SQLite。**PostgreSQL 后端分支当前
未实现**,设 `postgresql://...` 会在启动时报错。

## 测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
python -m ruff check app tests
python -m mypy app
```

Windows 启动脚本测试需要 PowerShell。需要真实模型或外部服务的集成测试，
应在配置好对应依赖后单独运行；单元测试通过不代表外部服务已经验证。

## 进一步重构

见 [重构清单](./docs/app_refactor_inventory.md):已完成项、待办优先级、以及巨型函数/文件
的安全拆分方法与路线图。

## License

Released under the [MIT License](./LICENSE).
Copyright (c) 2026 **bh1234666**.
