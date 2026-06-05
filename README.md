# Chatbot 后端

> Author: **bh1234666** · License: [MIT](./LICENSE)

群聊机器人后端:FastAPI + 多层记忆(hot/warm/cold/kb)+ LLM 编排 + 工具执行/委派 + OCR + 后台 "dream" 空闲整理子系统。

## 快速开始

```bash
# 1. 安装依赖
make install            # 运行时;开发用 make dev-install

# 2. 配置
cp .env.example .env    # 填入 DEEPSEEK_API_KEY 等

# 3. 启动(注意:必须单 worker,见下)
make run                # uvicorn app.main:app --workers 1
```

健康检查 `GET /health`,Prometheus 指标 `GET /metrics`。

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
`REDIS_URL` 但尚未接线;水平扩展需先把上述状态迁移到 Redis(见 `REFACTORING.md`)。

## 数据库

默认 SQLite(`DATABASE_URL=sqlite:///chatbot.db`)。代码内 SQL 用 PostgreSQL 语法
书写,运行时由 `app/db/sql_translate.py` 翻译为 SQLite。**PostgreSQL 后端分支当前
未实现**,设 `postgresql://...` 会在启动时报错。

## 测试

```bash
make test     # pytest;当前覆盖纯逻辑模块(如 SQL 翻译器差分测试)
make lint     # ruff
make typecheck
```

离线说明:`conftest.py` 为重依赖提供最小桩,使纯逻辑模块的测试无需安装全部
运行时依赖即可运行(便于 CI lint 阶段)。需要真实行为的集成测试请在装好依赖的
环境运行。

## 进一步重构

见 [`REFACTORING.md`](./REFACTORING.md):已完成项、待办优先级、以及巨型函数/文件
的安全拆分方法与路线图。

## License

Released under the [MIT License](./LICENSE).
Copyright (c) 2026 **bh1234666**.
