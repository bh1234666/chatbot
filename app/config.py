"""
全局配置。所有可调参数集中在此。
环境变量覆盖默认值（DEEPSEEK_API_KEY 等）。
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    # ── LLM ──
    # NOTE: model_name / lite_model_name are legacy fallback fields.
    # The primary model selection is now in app.llm.model_pool.
    # Settings below are only used when model_spec is not passed to client functions.
    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = "https://api.deepseek.com"
    gpt55_api_key: str = Field(default="", validation_alias="GPT55_API_KEY")
    gpt55_base_url: str = Field(default="", validation_alias="GPT55_BASE_URL")
    model_name: str = "deepseek-v4-pro"
    lite_model_name: str = "deepseek-v4-flash"  # fast/cheap model for summaries, narrations
    llm_provider_max_concurrent_default: int = Field(
        default=128,
        validation_alias="LLM_PROVIDER_MAX_CONCURRENT_DEFAULT",
    )
    llm_gpt55_max_concurrent: int = Field(
        default=128,
        validation_alias="LLM_GPT55_MAX_CONCURRENT",
    )
    llm_deepseek_max_concurrent: int = Field(
        default=128,
        validation_alias="LLM_DEEPSEEK_MAX_CONCURRENT",
    )
    llm_call_timeout_sec: float = Field(
        default=180.0,
        validation_alias="LLM_CALL_TIMEOUT_SEC",
    )
    llm_stream_first_chunk_timeout_sec: float = Field(
        default=180.0,
        validation_alias="LLM_STREAM_FIRST_CHUNK_TIMEOUT_SEC",
    )
    llm_stream_idle_timeout_sec: float = Field(
        default=300.0,
        validation_alias="LLM_STREAM_IDLE_TIMEOUT_SEC",
    )
    llm_main_stream_stall_timeout_sec: float = Field(
        default=240.0,
        validation_alias="LLM_MAIN_STREAM_STALL_TIMEOUT_SEC",
    )
    llm_environment_main_stream_stall_timeout_sec: float = Field(
        default=360.0,
        validation_alias="LLM_ENVIRONMENT_MAIN_STREAM_STALL_TIMEOUT_SEC",
    )

    # ── DB ──
    # ── 2026-05-07 Bug 2 fix：默认切回 sqlite ──
    # PostgreSQL asyncpg 分支未实现，2026-05-04 改的 PG 默认值会让新部署
    # 连接到不存在/配置错的 PG 实例。_db_path() 对非 sqlite URL 现在直接
    # raise RuntimeError(不再 silent fallback)，让配置错误在启动时就暴露。
    # 需 PG 的生产环境：在 .env 设 DATABASE_URL=postgresql://...，然后
    # 实现 pool.py 的 asyncpg 分支(见 Phase 4 可选任务)。
    database_url: str = Field(
        default="sqlite:///chatbot.db",
        validation_alias="DATABASE_URL",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias="REDIS_URL",
    )

    # ── 记忆容量 ──
    hot_user_turns: int = 80         # 用户热记忆轮数（一轮=一对 user/assistant）
    hot_group_events: int = 120      # 群组热记忆条数
    warm_user_max: int = 300
    warm_group_max: int = 500

    # 冷记忆呈现给模型的索引数量上限（headline 列入 system）
    cold_user_index_topn: int = 100
    cold_group_index_topn: int = 100
    kb_index_topn: int = 200

    # 冷压缩触发：温记忆超过此倍数 max 时压缩超出量
    # 触发阈值 = warm_user_max（>warm_user_max 即压缩超出部分）
    # 留参数化，运营可根据实际调
    warm_to_cold_batch: int = 30     # 单次压缩最旧 N 条温记忆

    # KB 压缩触发：未处理群消息数 >= 此值
    kb_compress_threshold: int = 50
    kb_compress_batch: int = 50      # 单次压缩最早 N 条群消息

    # Salience 时间衰减半衰期（天）。effective = salience * exp(-Δt/τ)
    salience_half_life_days: float = 7.0

    # 单次访问的 salience 增益
    salience_access_boost: float = 0.05

    # ── 注入防护标记 ──
    memory_injection_marker: str = "[SYSTEM_MEMORY_INJECTION/v1]"

    # ── 服务 ──
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    strict_active_archive: bool = Field(
        default=True,
        validation_alias="STRICT_ACTIVE_ARCHIVE",
    )
    napcat_url: str = Field(
        default="http://localhost:8099",
        validation_alias="NAPCAT_URL",
    )
    chatbot_url: str = Field(
        default="http://localhost:8000",
        validation_alias="CHATBOT_URL",
    )

    # ── 工作区 ──
    workspace_root: str = ""  # 空=自动用 data/workspaces/（相对于项目根目录）
    workspace_agent_max_bytes: int = Field(
        default=4 * 1024 * 1024 * 1024,
        validation_alias="WORKSPACE_AGENT_MAX_BYTES",
    )
    # workspace/env 子进程总内存预算。默认 16GiB，防止 benchmark/生成脚本并发撑爆 OS。
    workspace_run_memory_limit_bytes: int = Field(
        default=16 * 1024 * 1024 * 1024,
        validation_alias="WORKSPACE_RUN_MEMORY_LIMIT_BYTES",
    )
    # 系统剩余内存低于该值时，主进程会按顺序中断 workspace/env 子进程并把事实返回给 LLM。
    workspace_run_min_available_memory_bytes: int = Field(
        default=1024 * 1024 * 1024,
        validation_alias="WORKSPACE_RUN_MIN_AVAILABLE_MEMORY_BYTES",
    )

    # ── Debug ──
    # 开启时将 debug 事件送给廉价模型总结为一句话状态报告，仅输出状态不输出 payload。
    # 生产环境应保持关闭。
    debug_mode: bool = Field(default=False, validation_alias="DEBUG_MODE")
    # 详细模式：开启后恢复完整 payload 输出到控制台（prompt/response/tool results 全部打印）。
    # 仅当 debug_mode=True 时生效。输出量很大且含敏感信息。
    debug_verbose: bool = Field(default=False, validation_alias="DEBUG_VERBOSE")
    # 将完整 debug 日志（含所有 payload、工具调用及返回）写入指定目录。
    # 每个服务进程启动时创建带时间戳的日志文件。空字符串表示不写文件。
    # 注意：日志文件不含 ANSI 颜色码，适合 grep / 编辑器查看。
    debug_log_dir: str = Field(default="", validation_alias="DEBUG_LOG_DIR")
    # debug 报告用的模型（轻量、快速）。默认用 lite 模型。
    debug_model: str = ""  # 空字符串表示 fallback 到 lite_model_name
    # debug 输出中单个 payload 的最大字符数（防止超长 prompt 把终端刷爆）
    debug_payload_max_chars: int = 8000
    # 控制台 debug 输出。默认只在 stderr 是交互终端时输出；后台/管道场景下避免同步写 stderr 阻塞请求。
    debug_console: bool = Field(default=False, validation_alias="DEBUG_CONSOLE")
    # 是否在 prompt cache shape 日志中写入完整 per-message 结构。质量优化阶段默认保留完整内容。
    debug_prompt_cache_full_shape: bool = Field(
        default=True,
        validation_alias="DEBUG_PROMPT_CACHE_FULL_SHAPE",
    )
    # debug 输出是否使用 ANSI 颜色（仅当 stderr 是 tty 时生效）
    debug_color: bool = True

    # ── L7-1 (2026-05-09): 工具结果时间戳模式 ──
    # "full"(默认): 所有工具结果都注入时间戳字段
    # "minimal": 仅在慢(>5s)/失败时才注入,节省 5-15K tokens/trace
    tool_result_timestamp_mode: str = Field(
        default="full",
        validation_alias="TOOL_RESULT_TIMESTAMP_MODE",
    )

    # ── Patch 14: Round 3 在 easy 路径用 lite 模型 ──
    # 真实 trace 数据:trace 1 (你好) Round 3 用 pro 13 秒输出 26 字,TTFT 占 93%。
    # easy 路径 + 短回复用 lite 可降到 ~1.5-3s,质量损失对闲聊几乎不可察觉。
    # 默认 True;若发现人设质量回归严重可设为 False 关闭。
    lite_round3_for_easy: bool = Field(
        default=True,
        validation_alias="LITE_ROUND3_FOR_EASY",
    )
    voice_classifier_timeout_sec: float = Field(
        default=8.0,
        validation_alias="VOICE_CLASSIFIER_TIMEOUT_SEC",
    )

    # ── 2026-05-15 运维可调阈值(集中管理,避免散落代码)──
    # 这些常量原先散在 delegate.py / orchestrator.py / registry.py / api/chat.py 里,
    # 改一次=改代码+重新部署。挪到 settings 后,生产可以靠环境变量热调,
    # 默认值与旧硬编码完全一致(零行为变化)。
    # Helper 控制
    max_delegate_tasks_per_call: int = Field(default=16, validation_alias="MAX_DELEGATE_TASKS_PER_CALL")
    max_helpers_per_agent: int = Field(default=32, validation_alias="MAX_HELPERS_PER_AGENT")
    max_helpers_concurrent: int = Field(default=64, validation_alias="MAX_HELPERS_CONCURRENT")
    max_helper_depth: int = Field(default=2, validation_alias="MAX_HELPER_DEPTH")
    helper_long_run_observe_sec: int = Field(default=1800, validation_alias="HELPER_LONG_RUN_OBSERVE_SEC")
    helper_hard_kill_sec: int = Field(default=2700, validation_alias="HELPER_HARD_KILL_SEC")

    # Macro escalation (orchestrator round2)
    macro_hard_iter: int = Field(default=50, validation_alias="MACRO_HARD_ITER")
    macro_hard_time_sec: int = Field(default=1800, validation_alias="MACRO_HARD_TIME_SEC")
    macro_yellow_iter: int = Field(default=30, validation_alias="MACRO_YELLOW_ITER")
    macro_yellow_time_sec: int = Field(default=900, validation_alias="MACRO_YELLOW_TIME_SEC")

    # 资源并发(OCR / TTS 子进程内存沉重；GPU 默认全局串行，避免 OCR/TTS/Dream 叠加爆显存)
    ocr_concurrency: int = Field(default=1, validation_alias="OCR_CONCURRENCY")
    tts_concurrency: int = Field(default=1, validation_alias="TTS_CONCURRENCY")
    gpu_concurrency: int = Field(default=3, validation_alias="GPU_CONCURRENCY")
    mineru_concurrency: int = Field(default=2, validation_alias="MINERU_CONCURRENCY")
    umiocr_concurrency: int = Field(default=1, validation_alias="UMIOCR_CONCURRENCY")
    gpu_memory_budget_mb: int = Field(default=8000, validation_alias="GPU_MEMORY_BUDGET_MB")
    gpu_ocr_memory_mb: int = Field(default=0, validation_alias="GPU_OCR_MEMORY_MB")
    gpu_mineru_memory_mb: int = Field(default=1500, validation_alias="GPU_MINERU_MEMORY_MB")
    gpu_umiocr_memory_mb: int = Field(default=500, validation_alias="GPU_UMIOCR_MEMORY_MB")
    gpu_tts_memory_mb: int = Field(default=2500, validation_alias="GPU_TTS_MEMORY_MB")
    startup_ocr_warm_enabled: bool = Field(default=True, validation_alias="STARTUP_OCR_WARM_ENABLED")

    # 幂等去重窗口(api/chat.py)
    idempotency_ttl_sec: float = Field(default=30.0, validation_alias="IDEMPOTENCY_TTL_SEC")
    idempotency_max_entries: int = Field(default=5000, validation_alias="IDEMPOTENCY_MAX_ENTRIES")

    # ── 2026-05-16 Dream 后台整理 ──
    # Dream = 后台空闲时的智能整理 (升级版 maintenance)。
    # 信息量驱动 (各任务跟踪自己的"信息水位"), 不是时间驱动。
    # 学习现有 bg_tasks.schedule() 模式: fire-and-forget, 跟主线程并行, 默认不打断。
    dream_enabled: bool = Field(default=True, validation_alias="DREAM_ENABLED")
    # 单 dream 任务总超时 (秒, 防卡死)
    dream_task_timeout_sec: int = Field(default=600, validation_alias="DREAM_TASK_TIMEOUT_SEC")
    # 单 step 超时 (秒, checkpoint 任务用)
    dream_step_timeout_sec: int = Field(default=30, validation_alias="DREAM_STEP_TIMEOUT_SEC")
    # 单任务工具链上限 (步数, 防被反复打断永不完成)
    dream_max_steps_per_task: int = Field(default=30, validation_alias="DREAM_MAX_STEPS_PER_TASK")
    # 被打断多少次降级到 lite 模型 (放弃质量保完成)
    dream_interrupt_demote_threshold: int = Field(default=5, validation_alias="DREAM_INTERRUPT_DEMOTE_THRESHOLD")
    # dream cache 目录 (相对工作区根, 用于 checkpoint)
    dream_cache_subdir: str = Field(default=".dream_cache", validation_alias="DREAM_CACHE_SUBDIR")
    # 紧急 cancel: 工作区超此 MB 立即 cancel 所有 dream 任务
    dream_emergency_workspace_mb: int = Field(default=4000, validation_alias="DREAM_EMERGENCY_WORKSPACE_MB")
    # idle 判定: 主线程多久无活动算空闲 (秒). dream 仅在 idle 时启动新任务。
    dream_idle_threshold_sec: int = Field(default=5, validation_alias="DREAM_IDLE_THRESHOLD_SEC")
    # 日志详细度: "minimal" (仅 error/warn 进控制台) | "normal" | "verbose"
    dream_log_level: str = Field(default="minimal", validation_alias="DREAM_LOG_LEVEL")
    # LLM 日预算 ($), 超后暂停 dream 的 LLM 类任务
    dream_llm_budget_usd_per_day: float = Field(default=5.0, validation_alias="DREAM_LLM_BUDGET_USD_PER_DAY")

settings = Settings()
