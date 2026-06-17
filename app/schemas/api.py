"""
对外 API 的请求/响应 schema，以及内部模块间的数据结构。
"""
from typing import Optional, Literal
from pydantic import BaseModel, Field
from datetime import datetime


# ── 人设文件列表 ────────────────────────────────────────────
class PersonaMetaResponse(BaseModel):
    id: str
    name: str
    description: str = ""


# ── 存档管理 ────────────────────────────────────────────────
class ArchiveCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    persona_id: Optional[str] = None


class ArchiveResponse(BaseModel):
    archive_id: str
    name: str
    created_at: datetime


# ── 人设管理 ────────────────────────────────────────────────
class PersonaUpdateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class PersonaResponse(BaseModel):
    archive_id: str
    content: str
    updated_at: datetime


# ── 聊天 ────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    archive_id: str = ""
    group_id: str = ""
    user_id: str
    user_name: str = Field(default="", max_length=64)
    message: str = Field(min_length=1, max_length=8000)
    client_msg_id: Optional[str] = None
    current_dir: str = Field(default="", max_length=2048)
    project_id: str = Field(default="", max_length=128)
    persona_id: str = Field(default="environment", max_length=128)
    attached_file_ids: list[str] = Field(default_factory=list, max_length=50)


class EnvironmentChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    user_name: str = Field(default="", max_length=64)
    message: str = Field(min_length=1, max_length=8000)
    current_dir: str = Field(default="", max_length=2048)
    project_id: str = Field(default="", max_length=128)
    persona_id: str = Field(default="environment", max_length=128)
    client_msg_id: Optional[str] = None
    attached_file_ids: list[str] = Field(default_factory=list, max_length=50)


class AgentProjectCreateRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=128)
    current_dir: str = Field(default="", max_length=2048)
    project_id: str = Field(default="", max_length=128)
    archive_id: str = Field(default="", max_length=128)
    group_id: str = Field(default="", max_length=128)


class AgentProjectUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=128)
    current_dir: Optional[str] = Field(default=None, max_length=2048)


class AgentProjectResponse(BaseModel):
    user_id: str
    project_key: str
    archive_id: str
    group_id: str
    root_dir: str = ""
    project_name: str = ""
    created_at: str = ""
    last_seen_at: str = ""


class InterruptMessageRequest(BaseModel):
    archive_id: str
    group_id: str
    user_id: str
    message: str = Field(min_length=1, max_length=8000)
    client_msg_id: Optional[str] = None
    current_dir: str = Field(default="", max_length=2048)
    project_id: str = Field(default="", max_length=128)


class AutoContinueCheckRequest(BaseModel):
    user_message: str = Field(min_length=1, max_length=12000)
    assistant_reply: str = Field(min_length=1, max_length=24000)
    recent_context: str = Field(default="", max_length=16000)
    auto_continue_elapsed_sec: float = Field(default=0.0, ge=0.0)
    max_auto_continue_sec: float = Field(default=1800.0, ge=0.0)


class AutoContinueCheckResponse(BaseModel):
    should_continue: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    continue_message: str = "继续"


# ── 群消息旁观（用于 KB 数据来源；机器人不响应） ─────────────────
class ObserveRequest(BaseModel):
    archive_id: str
    group_id: str
    user_id: Optional[str] = None      # 系统消息可为空
    user_name: str = Field(default="未知", max_length=64)
    content: str = Field(min_length=1, max_length=8000)
    addressed_bot: bool = False        # 仅作标记；前端若要触发对话请走 /chat


class ObserveResponse(BaseModel):
    message_id: int


# ── 内部数据结构 ─────────────────────────────────────────────
class HotMessage(BaseModel):
    """用户热记忆中的一条消息"""
    role: Literal["user", "assistant"]
    content: str
    turn_id: str
    created_at: datetime


class GroupEvent(BaseModel):
    """群组热记忆中的一条事件（已转写为第三人称）"""
    actor_user_id: Optional[str]
    actor_name: str
    narration: str
    created_at: datetime


class TendencyAnalysis(BaseModel):
    """Round1 输出"""
    tendencies: dict[str, float]
    rationale: str
    complexity: Literal["easy", "medium", "hard"] = "medium"
    # B6 修复 (2026-05-02): 编码任务标记。round1 lite 模型识别后填入,
    # round2 路径分发处见此 = True 时直接走 main 模型(不用 lite),
    # 因为 lite 写代码不靠谱(实测 trace 55a58558 lite 写 buddy 反复修不到根上)。
    is_coding_task: bool = False
    # 2026-05-06: 文档任务标记。写 docx/pptx/xlsx/论文/报告/PPT → True,
    # round2 注入硬约束强制 delegate 给匹配的 helper。
    is_document_task: bool = False
    # L8-2 (2026-05-09): needs_recall 时 LLM 输出的检索关键词和推荐记忆层。
    # _build_recall_hint 用这两个字段生成精准的 recall 指引。
    recall_topics: list[str] = Field(default_factory=list)
    recall_layers: list[str] = Field(default_factory=list)


class ResponsePlan(BaseModel):
    """Round2 输出"""
    intent: str
    key_points: list[str]
    tone: str
    length_hint: str
    avoid: list[str] = Field(default_factory=list)
    callbacks: list[str] = Field(default_factory=list)
    internal_note: str = ""
    deliverables: list[str] = Field(default_factory=list)  # AI 决定推送给用户的文件名列表
    voice_reply_text: str = ""  # Round2 显式指定: 用这段文本作为最终语音回复内容
    voice_reply_file: str = ""  # Round2 显式指定: 用已生成音频作为最终语音回复,不按普通文件附件推送
    delivery_partial: list[str] = Field(default_factory=list)  # L5-2 (2026-05-09): promote 失败的 deliverable 名
    upgrade_to_hard: bool = False  # medium 路径下模型觉得困难时可申请升级
    upgrade_to_veryhard: bool = False  # hard 路径下模型觉得极度困难时可申请升级
    # Round2-only route corrections. These refine later Round2 stages but must
    # never downgrade the workflow to easy.
    round2_complexity: Optional[Literal["medium", "hard"]] = None
    round2_needs_tools: Optional[bool] = None
    round2_needs_recall: Optional[bool] = None


# ── Bot 管理 ──────────────────────────────────────────────────
class BotJoinRequest(BaseModel):
    archive_id: str
    group_name: str = ""
    persona_label: str = ""


class BotPersonaAddRequest(BaseModel):
    archive_id: str
    label: str = ""


class BotPersonaItem(BaseModel):
    archive_id: str
    archive_name: str = ""
    persona_label: str
    created_at: datetime
    is_active: int = 0
    last_summary: str = ""
    last_summary_at: Optional[datetime] = None


class BotGroupResponse(BaseModel):
    group_id: str
    active_archive_id: Optional[str] = None
    participate: bool = False
    group_name: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    personas: list[dict] = Field(default_factory=list)


class BotGroupListResponse(BaseModel):
    items: list[BotGroupResponse]


class CurrentArchiveRequest(BaseModel):
    archive_id: str


class AdminGroupRequest(BaseModel):
    group_id: str


# ── 群文件同步 ────────────────────────────────────────────────
class GroupFileItem(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    upload_time: int       # Unix timestamp
    uploader_uin: int = 0
    uploader_name: str = ""
    busid: int = 0


class GroupFilesSyncRequest(BaseModel):
    files: list[GroupFileItem] = Field(default_factory=list)


class GroupFilesSyncResponse(BaseModel):
    ok: bool = True
    synced: int = 0
