"""Primary orchestration entrypoint implementation."""
from __future__ import annotations

import re

from app.core.orchestrator_utils import _is_internal_deliverable_file


_ENV_PROJECT_PATH_RE = re.compile(
    r"(?<![\w.-])"
    r"(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_. -]+"
    r"\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|json|toml|ya?ml|ini|cfg|md|txt|rst|html|css|scss|less|vue|svelte|java|kt|go|rs|c|cc|cpp|h|hpp|cs|php|rb|sh|bat|ps1|sql|lock)"
    r"\b",
    re.IGNORECASE,
)
_ENV_ROOT_FILE_RE = re.compile(
    r"(?<![\w.-])"
    r"(?:README(?:\.[A-Za-z0-9]+)?|package\.json|pyproject\.toml|requirements(?:-[\w.-]+)?\.txt|setup\.py|setup\.cfg|Cargo\.toml|go\.mod|pom\.xml|build\.gradle|Makefile|Dockerfile|docker-compose\.ya?ml|tsconfig\.json|vite\.config\.[\w.]+|pytest\.ini)"
    r"(?![\w.-])",
    re.IGNORECASE,
)
_ENV_PROJECT_NOUNS = (
    "project", "repo", "repository", "codebase", "workspace", "directory", "folder",
    "file", "files", "module", "modules", "package", "source tree",
    "项目", "工程", "仓库", "代码库", "工作区", "目录", "文件", "模块", "源码", "结构",
)
_ENV_PROJECT_ACTIONS = (
    "inspect", "read", "check", "analyze", "analyse", "review", "summarize", "map",
    "list", "tree", "count", "measure", "rank", "largest", "smallest", "size",
    "lines", "chars", "characters", "bytes", "loc", "run", "test", "build",
    "compile", "fix", "modify", "change", "edit", "implement", "add", "refactor",
    "debug", "verify", "validate", "compare",
    "看", "读取", "读", "检查", "分析", "总结", "概括", "列", "列出", "输出",
    "统计", "多少", "多大", "规模", "最大", "最小", "排行", "职责", "作用",
    "运行", "测试", "构建", "编译", "修复", "修改", "实现", "增加", "重构",
    "调试", "验证", "对比",
)
_ENV_CODE_ACTIONS = (
    "fix", "modify", "change", "edit", "implement", "add", "refactor", "debug",
    "test", "build", "compile", "patch", "rewrite", "maintain",
    "修复", "修改", "改", "实现", "增加", "新增", "重构", "调试", "测试",
    "构建", "编译", "维护", "补全",
)

_USER_VISIBLE_PROTOCOL_MARKERS = (
    "<｜｜DSML｜｜",
    "｜｜DSML｜｜",
    "<|DSML|",
    "<｜tool_calls｜>",
    "</｜tool_calls｜>",
    "tool_calls name=",
)
_USER_VISIBLE_INTERNAL_ACTION_RE = re.compile(
    r"<\s*(?:read|write|edit|glob|search|run|tool|env_[a-z_]+)\b",
    re.IGNORECASE,
)


def _looks_like_user_visible_protocol_text(text: str) -> bool:
    """Detect hidden tool/protocol fragments before streaming final text.

    This is a presentation safety check for Round3 output. It does not rewrite
    planning decisions or tool choices; it prevents internal protocol/action
    markup from becoming the user-facing answer.

    Round3 最终文本协议泄漏检测；只用于展示安全，不改工具决策。
    """
    value = str(text or "")
    if any(marker in value for marker in _USER_VISIBLE_PROTOCOL_MARKERS):
        return True
    return bool(_USER_VISIBLE_INTERNAL_ACTION_RE.search(value))


def _plan_fallback_user_reply(plan) -> str:
    """Build a concise user-facing reply if Round3 leaks protocol markup."""
    parts: list[str] = []
    intent = str(getattr(plan, "intent", "") or "").strip()
    key_points = [
        str(item).strip()
        for item in (getattr(plan, "key_points", None) or [])
        if str(item).strip()
    ]
    deliverables = [
        str(item).strip()
        for item in (getattr(plan, "deliverables", None) or [])
        if str(item).strip()
    ]
    partials = [
        str(item).strip()
        for item in (getattr(plan, "delivery_partial", None) or [])
        if str(item).strip()
    ]
    if intent:
        parts.append(f"我完成了这轮处理：{intent}")
    if key_points:
        parts.append("关键结果：")
        parts.extend(f"- {item}" for item in key_points[:8])
    if deliverables:
        parts.append("已生成或准备交付的文件：")
        parts.extend(f"- {item}" for item in deliverables[:8])
    if partials:
        parts.append("仍需补齐或未完成的部分：")
        parts.extend(f"- {item}" for item in partials[:8])
    if not parts:
        parts.append("这轮工具链已经结束，但最终回复生成异常。我没有展示异常协议内容；请继续或重试，我会基于已完成结果重新整理。")
    return "\n".join(parts).strip()


def _load_displayed_name_remap_for_delivery(*workspaces: str | None) -> dict[str, str]:
    """Load user-facing filename remaps from the active delivery workspaces.

    Helper copy-back writes `.helpers_displayed_name.json` beside promoted files
    in the persistent workspace, while Round3 and done-file assembly may still
    be operating from the turn temp workspace. Delivery should read both maps;
    this does not choose or remove files, it only applies the naming decision
    already recorded by the helper/main workflow.

    读取主区和临时区的显示名映射；只修正文件显示名，不改变交付文件选择。
    """
    from app.llm.tools import workspace as _ws_tool

    merged: dict[str, str] = {}
    for workspace in workspaces:
        if not workspace:
            continue
        try:
            data = _ws_tool.load_displayed_name_remap(workspace) or {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str) and key and value:
                merged[key] = value
    return merged


def _environment_project_tool_route(message: str) -> tuple[bool, bool, str]:
    """Classify project-mode turns that need real project tools.

    Environment mode gives the model a real directory. When a user asks about
    project paths, project structure, source metrics, or maintenance work, an
    easy no-tool route produces plausible but ungrounded answers. This detector
    is deliberately scoped to environment mode by the caller.

    bot 项目模式路由：识别真实项目路径、结构统计和代码维护请求，进入工具链以获得证据。
    """
    text = (message or "").strip()
    if not text:
        return False, False, ""
    lowered = text.lower()
    has_path = bool(_ENV_PROJECT_PATH_RE.search(text) or _ENV_ROOT_FILE_RE.search(text))
    has_project_noun = any(term in lowered for term in _ENV_PROJECT_NOUNS)
    has_action = any(term in lowered for term in _ENV_PROJECT_ACTIONS)
    has_code_action = any(term in lowered for term in _ENV_CODE_ACTIONS)
    asks_capability_about_directory = (
        any(term in text for term in ("能看到", "能不能看到", "可以看到", "看得到", "当前目录", "所在目录"))
        and any(term in lowered for term in ("目录", "文件", "工程", "项目", "directory", "folder", "project", "files"))
    )
    if has_path and (has_action or has_code_action or any(term in lowered for term in ("helper", "delegate", "spawn"))):
        return True, has_code_action or bool(re.search(r"\.(?:py|js|ts|tsx|jsx|c|cpp|h|hpp|rs|go|java)\b", lowered)), "project path with action"
    if has_project_noun and has_action:
        return True, has_code_action, "project noun with action"
    if asks_capability_about_directory:
        return True, False, "directory visibility question"
    return False, False, ""


def _sync_orchestrator_globals() -> None:
    from app.core import orchestrator as _orchestrator
    globals().update({
        name: value
        for name, value in vars(_orchestrator).items()
        if not name.startswith("__") and name != "orchestrate"
    })


def _is_new_audio_file_request(message: str) -> bool:
    msg = (message or "").lower()
    if not msg:
        return False
    audio_terms = (
        "语音文件", "音频文件", "生成语音", "合成语音", "输出语音",
        "做个语音", "做一段语音", "生成音频", "合成音频", "输出音频",
        "tts", "wav", "mp3", "ogg", "m4a", "audio file",
        "voice file", "generate audio", "synthesize audio",
    )
    reuse_terms = (
        "重发", "复用", "再发", "刚才那个", "之前那个", "现有",
        "不用重新生成", "不要重新生成", "reuse", "resend",
    )
    return _is_audio_file_artifact_request(message) and not any(term in msg for term in reuse_terms)


def _is_audio_file_artifact_request(message: str) -> bool:
    """Return true when the user asks for an audio artifact, not voice reply style."""
    msg = (message or "").lower()
    if not msg:
        return False
    voice_reply_terms = (
        "语音回复", "用语音回复", "说给我听", "读给我听", "用声音回复",
        "voice reply", "reply by voice", "say it to me",
    )
    if any(term in msg for term in voice_reply_terms):
        return False
    audio_noun = (
        "语音", "音频", "声音", "wav", "mp3", "ogg", "m4a", "tts",
        "audio", "voice file",
    )
    artifact_terms = (
        "文件", "附件", "下载", "保存", "导出", "发给", "发送", "重发", "再发",
        "file", "attachment", "download", "save", "export", "send", "resend",
    )
    action_terms = (
        "生成", "输出", "合成", "做", "创建", "弄", "发", "给我",
        "generate", "output", "synthesize", "create", "make",
    )
    has_audio = any(term in msg for term in audio_noun)
    if not has_audio:
        return False
    if any(term in msg for term in artifact_terms):
        return True
    # Covers phrasing such as "输出一段随机测试语音" where the file-ness is
    # implied by "output/generate audio" rather than an explicit 文件 suffix.
    return any(term in msg for term in action_terms)


async def _review_explicit_deliverables_with_warnings(
    plan,
    *,
    user_message: str,
    workspace_dir: str,
    files_before: set,
    warning_facts: list[str],
) -> None:
    """Let the model reconsider explicit deliverables when non-fatal warnings fire."""
    if not warning_facts or not workspace_dir or not getattr(plan, "deliverables", None):
        return
    selected = [str(f) for f in (plan.deliverables or []) if str(f).strip()]
    if not selected:
        return
    file_facts: list[dict] = []
    for fname in selected:
        full_path = os.path.join(workspace_dir, fname)
        fname_norm = fname.replace("\\", "/")
        files_before_norm = {
            str(x).replace("\\", "/") for x in (files_before or set())
        }
        fact = {
            "name": fname,
            "basename": os.path.basename(fname_norm),
            "extension": os.path.splitext(fname_norm)[1].lower(),
            "exists": bool(os.path.isfile(full_path)),
            "preexisting_at_round_start": (
                fname in files_before
                or fname_norm in files_before_norm
                or os.path.basename(fname_norm) in {os.path.basename(x) for x in files_before_norm}
            ),
            "boundary_warning": _is_internal_deliverable_file(fname),
        }
        try:
            if fact["exists"]:
                fact["size_bytes"] = os.path.getsize(full_path)
        except OSError:
            pass
        file_facts.append(fact)

    messages = [
        {
            "role": "system",
            "content": (
                "You review user-facing file delivery decisions.\n"
                "The current user message is the delivery mainline. Conversation history, recent activity, workspace listings, "
                "and previous delivery lists are evidence only; they do not make old filenames current deliverables by themselves.\n"
                "`current_deliverables` and `plan_*` fields are also review inputs, not commands to preserve every file. "
                "They may contain filenames selected from old assistant messages or broad workspace listings. "
                "Facts may warn that a selected file looks internal, staged, or pre-existing. "
                "Warnings are evidence, not commands. This is not a rubber-stamp step: compare each filename, "
                "its age, and boundary facts with the current user request and plan evidence. Keep a file only "
                "when it is a final artifact for the current request, or when the current request explicitly needs "
                "that pre-existing/source file as a deliverable. Drop unrelated old work, helper contracts, scratch "
                "evidence, or source/input files that are merely context for the current work.\n"
                "Return strict JSON only: {\"deliverables\":[filenames from current_deliverables only],\"reason\":\"brief factual reason\"}.\n\n"
                "交付复核：警告是事实，不是删除命令；逐项比较当前请求、文件时间和边界事实，只保留当前任务的最终交付物。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_message": user_message,
                    "current_deliverables": selected,
                    "warning_facts": warning_facts,
                    "file_facts": file_facts,
                    "plan_intent": getattr(plan, "intent", ""),
                    "plan_key_points": list(getattr(plan, "key_points", None) or [])[:8],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    try:
        from app.llm.model_pool import chat_json, resolve_task
        _spec = resolve_task("self_check_plan")
        raw = await asyncio.wait_for(
            chat_json(
                messages,
                model_spec=_spec,
                reasoning="disabled",
                metrics_tag="json.deliverable_warning_review",
            ),
            timeout=15.0,
        )
    except Exception as exc:
        debug.log(
            "workspace.deliverable_warning_review.failed",
            f"{type(exc).__name__}: keeping explicit plan.deliverables",
        )
        return
    if not isinstance(raw, dict):
        return
    allowed = set(selected)
    reviewed: list[str] = []
    for item in raw.get("deliverables") or []:
        text = str(item).strip()
        if text in allowed and text not in reviewed:
            reviewed.append(text)
    if not reviewed and raw.get("deliverables") not in ([], None):
        return
    if reviewed != selected:
        debug.log(
            "workspace.deliverable_warning_review.applied",
            "LLM revised explicit deliverables after warning facts",
            {
                "before": selected,
                "after": reviewed,
                "reason": str(raw.get("reason", ""))[:300],
            },
        )
        plan.deliverables = reviewed
    else:
        debug.log(
            "workspace.deliverable_warning_review.kept",
            str(raw.get("reason", "kept explicit deliverables"))[:300],
            {"deliverables": reviewed},
        )


def _existing_environment_project_files(names: set[str]) -> set[str]:
    """Return deliverable names that exist as real files in the environment project.

    Forced-finalize paths may only have a basename such as ``graph.py`` while
    the real environment file is ``src/pkg/graph.py``. Treat a basename as an
    existing project file only when it maps to exactly one file under the
    project root; ambiguous basenames remain unresolved so the normal missing
    delivery guard can be conservative.
    """
    if not names:
        return set()
    try:
        from pathlib import Path
        from app.core.runtime_mode import current_environment, is_environment_mode

        if not is_environment_mode():
            return set()
        env = current_environment()
        if env is None or not env.root_dir:
            return set()
        root = Path(env.root_dir).resolve()
    except Exception:
        return set()

    normalized_names = {
        str(name or "").strip().strip('"').replace("\\", "/")
        for name in names
        if str(name or "").strip().strip('"')
    }
    basename_hits: dict[str, int] = {}
    wanted_basenames = {
        raw
        for raw in normalized_names
        if "/" not in raw
    }
    if wanted_basenames:
        try:
            for child in root.rglob("*"):
                if not child.is_file():
                    continue
                bn = child.name
                if bn in wanted_basenames:
                    basename_hits[bn] = basename_hits.get(bn, 0) + 1
        except OSError:
            basename_hits = {}

    existing: set[str] = set()
    for name in names:
        raw = str(name or "").strip().strip('"').replace("\\", "/")
        if not raw:
            continue
        if "/" not in raw and basename_hits.get(raw) == 1:
            existing.add(name)
            continue
        try:
            candidate = Path(raw)
            path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
            path.relative_to(root)
        except Exception:
            continue
        try:
            if path.is_file():
                existing.add(name)
        except OSError:
            continue
    return existing


def _plan_quality_score(plan) -> int:
    """Rough completeness score used to prevent an upgraded stage from regressing."""
    if plan is None:
        return 0
    text = " ".join([str(getattr(plan, "intent", "") or "")] + [
        str(x) for x in (getattr(plan, "key_points", None) or [])
    ])
    score = len([x for x in (getattr(plan, "key_points", None) or []) if str(x).strip()])
    if len(text) >= 500:
        score += 2
    if len(text) >= 1000:
        score += 2
    evidence_markers = (
        "候选", "风险", "读取", "搜索", "验证", "结果", "统计", "app/",
        "bytes", "py", "函数", "类", "依赖", "拆分",
    )
    score += min(8, sum(1 for marker in evidence_markers if marker in text))
    preparation_markers = ("先读取", "先看", "下一步", "我来做", "准备")
    if any(marker in text for marker in preparation_markers) and len(text) < 600:
        score -= 8
    if any(term in text for term in ("read_multiple_files", "env_read(path=", "<div class=\"tool_call\"")):
        score -= 6
    return score


def _choose_non_regressed_plan(previous, current):
    """Keep previous plan when a stronger stage produced only a preparatory shell."""
    if previous is None or current is None:
        return current
    prev_score = _plan_quality_score(previous)
    cur_score = _plan_quality_score(current)
    if prev_score >= 10 and cur_score + 4 < prev_score:
        try:
            debug.log(
                "round2.upgrade_regression_guard",
                f"keeping prior plan: prev_score={prev_score} cur_score={cur_score}",
            )
        except Exception:
            pass
        previous.upgrade_to_hard = False
        previous.upgrade_to_veryhard = False
        return previous
    return current


async def orchestrate(
    req: ChatRequest,
    *,
    trace_id: Optional[str] = None,
    interrupt_messages_getter=None,
) -> AsyncIterator[tuple[str, dict]]:
    _sync_orchestrator_globals()

    trace_id = trace_id or uuid.uuid4().hex[:16]
    debug.set_trace_id(trace_id)
    try:
        from app.core import toolchain_cache as _toolchain_cache
        _toolchain_cache.reset_trace(trace_id)
    except Exception:
        pass
    # 2026-05-16 Round 14b: reset round3 parallel 决策 ContextVar
    # (ContextVar 通常 per-task 隔离, 但同进程 task 复用边界不确定, 显式清最稳)
    try:
        from app.llm.voice_output import _round3_parallel_decision
        _round3_parallel_decision.set("")
    except Exception:
        pass
    archive_row = None
    try:
        archive_row = await archive_dao.get_archive(req.archive_id)
    except Exception:
        log.exception("archive load failed")
    # 2026-05-16 Dream: 标记主线程活动 (dream 据此判定 idle, 不在 idle 时启动新任务)
    try:
        from app.core.dream import supervisor as _dream_sup
        _dream_sup.mark_main_activity()
    except ImportError:
        pass  # dream 模块不在 / 关闭 → 跳过
    debug.section(
        f"NEW CHAT  user={req.user_id} group={req.group_id} archive={req.archive_id}"
    )
    debug.log("orchestrate.start", f"user_name={req.user_name!r}", {"message": req.message})

    # 2026-05-02 part9 #16:阶段耗时聚合,最终在 complete event 里给前端
    # 让用户/运维能从前端就看到"哪一步慢",不用看 server log。
    _orch_start = _time.monotonic()
    _orch_wall_start = _time.time()
    _timing: dict[str, float] = {}
    def _mark(stage: str) -> None:
        _timing[stage] = round(_time.monotonic() - _orch_start, 3)

    yield "meta", {"trace_id": trace_id}

    direct_reply = _extract_direct_short_reply(req.message)
    if direct_reply:
        debug.log(
            "direct_short_reply.shortcut",
            "explicit literal reply request, skipping round1/round2/round3 LLM",
            direct_reply,
        )
        yield "token", {"text": direct_reply}
        yield "done", {
            "tendencies": {"任务类": 0.0, "社交类": 0.2},
            "trace_id": trace_id,
        }
        _mark("direct_done")
        # Keep the regular user-facing reply fast; memory writes are intentionally skipped
        # for literal echo probes to avoid spending more time than the answer itself.
        yield "complete", {
            "trace_id": trace_id,
            "timing": _timing,
            "elapsed_sec": round(_time.monotonic() - _orch_start, 3),
        }
        return

    light_workspace_literal = _is_light_workspace_list_with_literal_reply(req.message)
    if light_workspace_literal:
        literal_reply = _extract_requested_literal_reply(req.message)
        debug.log(
            "workspace_list_literal.shortcut",
            "simple workspace listing with requested literal final reply",
            literal_reply,
        )
        yield "progress", _progress_payload("planning_tools", "planning", "正在检查工作区")
        main_workspace_dir = ws_tool.create_workspace(
            archive_id=req.archive_id, group_id=req.group_id
        )
        ws_tool.archive_stale_artifacts(main_workspace_dir, max_age_days=14)
        session_tag = (
            f"{req.archive_id}:{req.group_id}:{req.user_id}:"
            f"{hash(req.message) & 0xFFFFFFFF:08x}"
        )
        workspace_dir = ws_tool.ensure_temp_workspace(main_workspace_dir, session_tag=session_tag)
        group_key = f"{req.archive_id}:{req.group_id}"
        ws_tool.register_workspace(group_key, workspace_dir)
        try:
            result = await ws_tool.handle_run(workspace_dir, "dir", timeout_sec=5)
            debug.log("workspace_list_literal.result", "dir completed", result)
        except Exception as e:
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            debug.log("workspace_list_literal.error", result["error"])
        plan = ResponsePlan(
            intent="检查当前工作区并按用户指定字面回复",
            key_points=["工作区目录检查成功" if result.get("ok") else f"工作区目录检查失败: {result.get('error', '')}"],
            tone="简短直接",
            length_hint="短",
            internal_note="light_workspace_list_with_literal_reply",
        )
        final_text = literal_reply
        yield "token", {"text": final_text}
        _mark("direct_workspace_done")
        bot_log_payload = _build_bot_log(
            plan,
            [],
            "medium",
            False,
            promoted_to_main=[],
            helper_status={},
            internal_note=plan.internal_note,
        )
        stored_assistant = (
            final_text + f"\n\n<bot_log>{bot_log_payload}</bot_log>"
            if bot_log_payload else final_text
        )
        done_payload = {
            "tendencies": {"任务类": 0.6, "轻量工具": 0.9},
            "trace_id": trace_id,
        }
        yield "done", done_payload
        yield "progress", _progress_payload("updating_memory", "maintaining", "正在更新记忆")
        try:
            await _post_response_maintenance(
                req=req,
                user_message=req.message,
                assistant_message=stored_assistant,
                tendencies=done_payload["tendencies"],
                plan=plan,
                trace_id=trace_id,
                generated_files=[],
                workspace_dir=workspace_dir,
                progress_messages=[],
                finalize_and_compress=_bg_finalize_and_compress,
                debug=debug,
            )
        except Exception as e:
            log.exception("[%s] light workspace maintenance failed", trace_id)
            debug.log("workspace_list_literal.maintenance_error", f"{type(e).__name__}: {e}")
        finally:
            try:
                ws_tool.unregister_workspace(group_key)
            except Exception:
                pass
        yield "complete", {
            "trace_id": trace_id,
            "timing": _timing,
            "elapsed_sec": round(_time.monotonic() - _orch_start, 3),
        }
        return

    # ── 0. 并行预加载所有可读的记忆 + 人设 ──
    # 加速策略：先单独启动 hot_user 加载（Round 1 的唯一依赖），
    # 立即启动 Round 1，与剩余 7 路记忆/人设加载并行。
    # cold/kb 索引带 julianday/exp 计算是这一组里最慢的，
    # Round 1 lite 模型一次调用大概率比它们更快——pipeline 后 Round 1 几乎"免费"。
    yield "progress", _progress_payload("loading_memory", "loading", "正在加载记忆和人设")
    debug.log("load.start", "fetching memories+persona in parallel; round1 pipelined")

    # 2026-05-02 part7:并行起飞读上次的暂停快照(用户上次主动 abort 时持久化的状态)。
    # 通常文件不存在(没暂停过)→ 立即返回 None,不影响延迟。
    # 存在时 inject 到 base_msgs 动态 user 区,告诉模型上次 paused 了哪些 helper。
    pause_snapshot_task = asyncio.create_task(
        _pause_state.load_pause(
            archive_id=req.archive_id, group_id=req.group_id, user_id=req.user_id,
        )
    )

    # 2026-05-02 part10 F1:并行起飞读 user profile(per archive × user)。
    # 通常 chat 数 < EXTRACTION_INTERVAL 时还没建,返回 None。
    # 存在时 inject 到 base_msgs 动态 user 区,让 round2/round3 看到用户偏好。
    user_profile_task = asyncio.create_task(
        _user_profile.load_profile(
            archive_id=req.archive_id, user_id=req.user_id,
        )
    )

    hot_user_task = asyncio.create_task(
        hot.load_user_hot(req.archive_id, req.group_id, req.user_id)
    )

    # #10 修:trivial 路径不加载完整记忆,省 7 个 db query (~300-500ms)
    # 仅依赖 hot_user (Round 3 需要) + persona (人设回复需要)。
    # 其他流(warm/cold/kb/files/hot_group)对单字问候完全不需要。
    is_trivial = _is_trivial_message(req.message)
    if is_trivial:
        debug.log("load.trivial_skip",
                  "trivial message, skipping warm/cold/kb/files load")
        persona_task = asyncio.create_task(archive_dao.get_persona(req.archive_id))
        # 其他记忆全置空,后续逻辑不会用到(easy plan + 直接 Round 3)
        others_task = None
        # 占位空值,Round 3 从这些里读会得到空结果
        hot_group: list = []
        warm_user_idx: list = []
        warm_group_idx: list = []
        cold_user_idx: list = []
        cold_group_idx: list = []
        kb_idx: list = []
        file_idx: list = []
        recent_group_msgs: list = []
    else:
        others_task = asyncio.gather(
            hot.load_group_hot(req.archive_id, req.group_id),
            warm.load_user_warm_index(req.archive_id, req.group_id, req.user_id),
            warm.load_group_warm_index(req.archive_id, req.group_id),
            cold.load_cold_user_index(req.archive_id, req.group_id, req.user_id),
            cold.load_cold_group_index(
                req.archive_id, req.group_id,
                viewer_user_id=req.user_id,
            ),
            kb.load_kb_index(
                req.archive_id, req.group_id,
                viewer_user_id=req.user_id,
            ),
            kb.load_file_index(
                req.archive_id, req.group_id,
                viewer_user_id=req.user_id,
            ),
            archive_dao.get_persona(req.archive_id),
            # 群内最近原话(per-user 并行下"群总览"的关键数据源,
            # 弥补 group_events 只看到 bot 参与事件的盲区)
            # 2026-05-11 主进程精简: 30 → 15 (远期 11+ 也已截到 100 字符, 净减约 4-5KB)
            gm.load_recent(req.archive_id, req.group_id, limit=15),
            return_exceptions=True,
        )
        persona_task = None

    hot_user = await hot_user_task

    # ── Bug 11 修:trivial 消息短路,跳过 Round 1 的 12s LLM 调用 ──
    # log 实测 14 个 trace 中 12 个 Round 1 都耗 12+ 秒(DeepSeek lite 冷启动 prefix
    # cache miss),easy 路径整段时长 14-17s 中 80% 是 Round 1。
    # 简单问候/确认句直接走预设 easy,主对话流程立刻进 Round 3。
    if is_trivial:
        debug.log("round1.shortcut", f"trivial message, skipping round1 LLM",
                  req.message[:80])
        round1_task = None  # 不发起 LLM 调用
    else:
        # 立即开始 Round 1（与剩余 memory 加载并行）
        yield "progress", _progress_payload("analyzing_intent", "analyzing", "正在分析用户意图")
        debug.section("ROUND 1 — tendency analysis (parallel with memory load)")
        round1_task = asyncio.create_task(_round1(req.user_name, req.message, hot_user))

    # 等记忆完成
    if others_task is not None:
        results = await others_task

        def _safe_mem(idx, default):
            r = results[idx]
            if isinstance(r, BaseException):
                log.warning("memory load #%d failed: %r; using default", idx, r)
                return default
            return r

        hot_group         = _safe_mem(0, [])
        warm_user_idx     = _safe_mem(1, [])
        warm_group_idx    = _safe_mem(2, [])
        cold_user_idx     = _safe_mem(3, [])
        cold_group_idx    = _safe_mem(4, [])
        kb_idx            = _safe_mem(5, [])
        file_idx          = _safe_mem(6, [])
        persona           = _safe_mem(7, {})
        recent_group_msgs = _safe_mem(8, [])
    else:
        # trivial 路径只等 persona
        persona = await persona_task
    debug.log(
        "load.done",
        f"hot_user={len(hot_user)} hot_group={len(hot_group)} "
        f"warm_user={len(warm_user_idx)} warm_group={len(warm_group_idx)} "
        f"cold_user={len(cold_user_idx)} cold_group={len(cold_group_idx)} "
        f"kb={len(kb_idx)} files={len(file_idx)} "
        f"recent_group_msgs={len(recent_group_msgs)}",
    )

    # 等 Round 1（通常此时已完成);trivial 路径直接造一个固定结果
    if round1_task is None:
        r1 = Round1Result(
            tendency=TendencyAnalysis(
                tendencies={"闲聊": 0.9},
                rationale="trivial 短路,跳过 LLM",
                complexity="easy",
            ),
            needs_tools=False,
            needs_recall=False,
            parallelizable=False,
        )
    else:
        r1 = await round1_task
    tendency_obj = r1.tendency
    complexity = tendency_obj.complexity
    needs_tools = r1.needs_tools
    needs_recall = r1.needs_recall
    parallelizable = r1.parallelizable
    log.info(
        "[%s] round1 tendencies=%s complexity=%s needs_tools=%s needs_recall=%s parallelizable=%s",
        trace_id, tendency_obj.tendencies, complexity, needs_tools, needs_recall, parallelizable,
    )
    debug.log(
        "round1.done",
        f"complexity={complexity} needs_tools={needs_tools} "
        f"needs_recall={needs_recall} parallelizable={parallelizable}",
        tendency_obj.tendencies,
    )
    _mark("round1_done")
    await debug.report()
    _td = tendency_obj.tendencies or {}
    _context_followup_intent = _has_context_followup_intent(req.message)
    _constraint_review_followup_intent = _has_current_turn_constraint_review_language(req.message)
    _tool_concept_intent = _is_tool_concept_question(req.message)
    _env_project_tool_intent = False
    _env_project_coding_intent = False
    _env_project_route_reason = ""
    _audio_artifact_intent = _is_audio_file_artifact_request(req.message)
    try:
        from app.core.runtime_mode import is_environment_mode as _is_environment_mode
        if _is_environment_mode():
            (
                _env_project_tool_intent,
                _env_project_coding_intent,
                _env_project_route_reason,
            ) = _environment_project_tool_route(req.message)
    except Exception:
        pass

    # per-user 并行下,本群当前还有哪些其他用户的请求没结束。
    # 拿 (user_id, user_name) 对,在 system 提示里展示人类可读的名字
    # (而不是 QQ 号)。GroupGuard.in_flight_users_in_group 现在内置 exclude_user_id
    # 参数,直接排除自己。
    try:
        in_flight_others = await get_group_guard().in_flight_users_in_group(
            req.archive_id, req.group_id, exclude_user_id=req.user_id,
        )
    except Exception:
        in_flight_others = []

    inline_images = _scan_inline_images(req.archive_id, req.group_id)
    _base_needs_static_context = (
        bool(needs_recall)
        or bool(needs_tools)
        or _context_followup_intent
        or _constraint_review_followup_intent
        or _td.get("严肃询问", 0) >= 0.7
        or any(
            kw in (req.message or "")
            for kw in ("刚才", "前面", "群里", "大家", "谁说", "讨论", "最近")
        )
        or _td.get("敌意", 0) > 0.3
        or _td.get("测试", 0) > 0.7
        or _has_implicit_recall_intent(req.message)
        or _has_image_intent_in_msg(req.message)
        or _audio_artifact_intent
    )
    if _base_needs_static_context:
        _ctx_hot_group = hot_group
        _ctx_warm_user_idx = warm_user_idx
        _ctx_warm_group_idx = warm_group_idx
        _ctx_cold_user_idx = cold_user_idx
        _ctx_cold_group_idx = cold_group_idx
        _ctx_kb_idx = kb_idx
        _ctx_file_idx = file_idx
        if not (needs_recall or needs_tools or _has_implicit_recall_intent(req.message)):
            _ctx_warm_user_idx = []
            _ctx_warm_group_idx = []
            _ctx_cold_user_idx = []
            _ctx_cold_group_idx = []
            _ctx_kb_idx = []
            _ctx_file_idx = []
            debug.log(
                "ctx.static_partial",
                "group-awareness turn: keeping hot_group only, omitting static indexes",
            )
    else:
        _ctx_hot_group = []
        _ctx_warm_user_idx = []
        _ctx_warm_group_idx = []
        _ctx_cold_user_idx = []
        _ctx_cold_group_idx = []
        _ctx_kb_idx = []
        _ctx_file_idx = []
        debug.log(
            "ctx.pruned",
            "no_tools/no_recall: static memory and KB indexes omitted from base context",
            {
                "complexity": complexity,
                "hot_group": len(hot_group),
                "warm_user": len(warm_user_idx),
                "warm_group": len(warm_group_idx),
                "cold_user": len(cold_user_idx),
                "cold_group": len(cold_group_idx),
                "kb": len(kb_idx),
                "files": len(file_idx),
            },
        )
    _base_needs_recent_context = (
        bool(needs_recall)
        or _has_implicit_recall_intent(req.message)
        or _context_followup_intent
        or _constraint_review_followup_intent
        or _td.get("严肃询问", 0) >= 0.7
        or _td.get("闲聊", 0) >= 0.5
        or _td.get("情感倾诉", 0) >= 0.5
        or any(
            kw in (req.message or "")
            for kw in ("刚才", "前面", "群里", "大家", "谁说", "讨论", "最近")
        )
    )
    if _base_needs_recent_context:
        _ctx_recent_group_msgs = recent_group_msgs
        _ctx_in_flight_others = in_flight_others
    else:
        _ctx_recent_group_msgs = []
        _ctx_in_flight_others = []
        if recent_group_msgs or in_flight_others:
            debug.log(
                "ctx.recent_pruned",
                "no recall/group-awareness need: recent group snapshot omitted",
                {
                    "complexity": complexity,
                    "recent_group_msgs": len(recent_group_msgs),
                    "in_flight_others": len(in_flight_others),
                },
            )
    base_msgs = ctx_build.build_base_context(
        user_name=req.user_name,
        current_message=req.message,
        hot_user=hot_user,
        hot_group=_ctx_hot_group,
        warm_user_index=_ctx_warm_user_idx,
        warm_group_index=_ctx_warm_group_idx,
        cold_user_topk=_ctx_cold_user_idx,
        cold_group_topk=_ctx_cold_group_idx,
        kb_topk=_ctx_kb_idx,
        file_index=_ctx_file_idx if _ctx_file_idx else None,
        in_flight_others=_ctx_in_flight_others or None,
        recent_group_messages=_ctx_recent_group_msgs or None,
        inline_images=inline_images,
    )
    debug.log("ctx.built", f"base_msgs={len(base_msgs)}", base_msgs)

    _constraint_review_requires_round2 = _should_route_constraint_review_to_round2(
        req.message,
        base_msgs,
    )
    if complexity == "easy" and _constraint_review_requires_round2:
        complexity = "medium"
        parallelizable = False
        debug.log(
            "round1.constraint_review_route",
            "current turn asks to compare prior task evidence with explicit constraints; keeping Round2 planning",
            {
                "needs_tools": bool(needs_tools),
                "needs_recall": bool(needs_recall),
            },
        )

    # 2026-05-02 part7:把暂停快照渲染成一段中文；后续统一放入 user 动态上下文，
    # 避免把 per-turn 状态追加到 system 前缀。
    # 没有上次暂停时 pause_snapshot 是 None — 跳过即可,零代价。
    pause_snapshot = None
    try:
        pause_snapshot = await pause_snapshot_task
    except Exception:
        log.exception("pause_state load failed (non-fatal)")
    # 2026-05-15 Item 3:这 4 段 per-turn 动态文本以前各自 append 到 system 尾部,
    # 破坏 prefix cache。改成"先收集 _pause_text / _profile_text / _feedback_text /
    # _lang_text",最后一次性塞进独立 user 消息(_inject_dynamic_session_info)。
    # 这里只收文本、不再碰 base_msgs。
    _pause_text = ""
    if pause_snapshot:
        _pause_text = _pause_state.render_pause_summary_for_prompt(pause_snapshot)
        if _pause_text.strip():
            debug.log(
                "pause_state.loaded",
                f"queued pause snapshot from {pause_snapshot.get('paused_at', '?')}: "
                f"{len(pause_snapshot.get('active_helpers') or [])} active helpers, "
                f"{len(pause_snapshot.get('completed_helpers') or [])} completed",
            )

    # ── 2026-05-02 part10 F1:user profile 注入 ──
    # 上次 chat maintenance 后台任务积累的用户偏好画像(代码风格、兴趣、不喜欢的话题等)。
    # 第一次见的用户没 profile → user_profile_task 返回 None,跳过。
    # 后续通过 SYSTEM_MEMORY_INJECTION 放入 user 动态上下文(和 pause_state、
    # feedback retry 用同一通道)。
    user_profile = None
    try:
        user_profile = await user_profile_task
    except Exception:
        log.exception("user_profile load failed (non-fatal)")
    _profile_text = ""
    if user_profile:
        _profile_text = _user_profile.render_profile_for_prompt(user_profile)
        if _profile_text.strip():
            debug.log(
                "user_profile.loaded",
                f"queued profile (chat_count={user_profile.get('chat_count', 0)}, "
                f"prefs={len(user_profile.get('preferences') or {})}, "
                f"interests={len(user_profile.get('interests') or [])})",
            )

    # ── 2026-05-02 part10 (F4):用户负反馈复盘 mode ──
    # 用户这次发言含明确不满 / 重做请求关键词,且上一轮 bot 有 <bot_log> 记录(说明
    # 上次回复有可追溯的执行细节)→ 注入"复盘提示"让 round2 先读上次的 bot_log
    # 找具体错因,本轮明确避开同样错误。设计:
    #   - 纯 prompt 引导,无新表 / 无新 LLM 调用
    #   - 触发条件保守(否定+重做+明确情绪标记 三类关键词,加 120 字长度上限)
    #   - 注入位置和 pause_state 同——写入动态 user 上下文
    # 不直接告诉模型"你错了",让它自己读 bot_log 判断—— bot_log 里有 deliverables/
    # complexity/was_aborted 等信号,模型可以基于事实纠错而非盲目 apologize。
    # 负反馈"复盘 mode"提示(构造逻辑见 orchestrator_prompts.build_feedback_retry_text)
    _feedback_text, _feedback_bot_log_found = build_feedback_retry_text(req.message, hot_user)
    if _feedback_text:
        debug.log(
            "feedback_retry.injected",
            f"negative feedback detected (bot_log_found={_feedback_bot_log_found}); "
            f"round2 will see retry-mode hint",
        )

    # ── 2026-05-02 part12 (Bug C):语言一致性硬约束 ──
    # 实测 trace 74b1295b:用户中文消息,round1/round3 输出中文,但主线程调
    # office.write 写 docx/pptx/xlsx 全部英文。根因:中间环节没有"用户语言"信号传递。
    # 解决:base_msgs 准备完成后,根据用户原 message 检测语言,写入动态会话信息。
    # round2 / round3 / 通过 delegate 给 helper 的 prompt 都看得见。
    _user_lang = _detect_user_language(req.message or "")
    # 2026-05-02 part12:set ContextVar,delegate 模块的 handle_delegate /
    # handle_spawn_helper 调用 _run_one_helper 时通过 current_user_lang() 读取,
    # 在 helper system prompt 末尾追加语言硬约束(_helper_lang_hint)。
    try:
        from app.llm.tools.delegate import set_current_user_lang as _set_user_lang
        _user_lang_token = _set_user_lang(_user_lang)
    except Exception:
        _user_lang_token = None

    # 2026-05-10 Patch 83: 设置人设守卫 ContextVar
    # 让 handle_delegate(spawn) 入口拿到 persona + user_message,启动并行守卫
    # 判断"角色是否同意做"。详见 tool_delegate._persona_consent_guard。
    _persona_excerpt_token = None
    _voice_instruct_token = None
    _voice_ref_token = None
    _user_msg_token = None
    try:
        from app.llm.tools import registry as _tool_registry
        from app.llm.tools.delegate import (
            set_current_persona_excerpt as _set_persona,
            set_current_user_message as _set_user_msg,
        )
        _persona_excerpt_token = _set_persona(persona or "")
        _tts_guard_token = _tool_registry.set_current_tts_guard_context(persona or "", req.message or "")
        from app.memory.persona_files import persona_voice_instruct_by_content
        _voice_instruct = persona_voice_instruct_by_content(
            persona or "",
            default=_extract_voice_instruct(persona or ""),
        )
        _voice_instruct_token = _tool_registry.set_current_voice_instruct(_voice_instruct)
        _persona_name = ""
        from app.memory.persona_files import find_persona_voice_sample_by_content
        _voice_ref = find_persona_voice_sample_by_content(persona or "")
        if _voice_ref is None:
            _persona_name = (archive_row or {}).get("name") or ""
            from app.memory.persona_files import find_persona_voice_sample
            _voice_ref = find_persona_voice_sample(_persona_name)
        _voice_ref_token = _tool_registry.set_current_voice_ref_audio(str(_voice_ref) if _voice_ref else "")
        debug.log("voice.ref", f"mode={'clone' if _voice_ref else 'design'} ref={str(_voice_ref) if _voice_ref else ''}")
        _user_msg_token = _set_user_msg(req.message or "")
    except Exception:
        pass
    _lang_directive = _language_directive(_user_lang)
    _lang_text = ""
    if _lang_directive:
        _lang_text = _lang_directive
        debug.log(
            "language.directive_injected",
            f"detected user language='{_user_lang}'; "
            f"queued language hard-constraint for SYSTEM_MEMORY_INJECTION",
        )

    try:
        from app.core.environment_prompt import (
            environment_project_context as _env_project_context,
            environment_prompt_addon as _env_prompt_addon,
        )
        _mode_text = _env_prompt_addon()
        _project_context_text = _env_project_context()
        if _mode_text:
            debug.log("environment.prompt.injected", "environment addon queued")
    except Exception:
        _mode_text = ""
        _project_context_text = ""

    _task_fact_parts: list[str] = []
    try:
        _current_is_coding_task = bool(getattr(tendency_obj, "is_coding_task", False))
    except Exception:
        _current_is_coding_task = False
    if _env_project_coding_intent or _current_is_coding_task:
        _task_fact_parts.append(
            "Current coding task contract: project path discovery facts from env_list_tree/env_inventory/env_search are enough to start a code helper. "
            "In environment project mode, workspace listings and workspace.locate are chat/staged-workspace facts, not real-project-root source/test search facts. "
            "The first code helper should receive likely project paths in input_files plus acceptance_checks; it owns source-body reading, optional baseline failure reproduction, diagnosis, edits, and test iteration. "
            "If the user explicitly requested browser/host-browser evidence, collect or delegate browser evidence once URL/path facts are known before broad source reads or edits. "
            "The main thread keeps route facts, env_diff/env_apply_*, and acceptance summaries compact; if a local URL is already running, staged helper edits affect it only after main-thread apply.\n"
            "当前代码任务契约：环境项目路径以 env_* 为准；显式浏览器任务路径/URL 已知后先取或委派浏览器证据；路径发现足以派发 code helper；helper 负责源码阅读、诊断、修改和测试；已有 URL 需主进程 apply 后才反映 _env 改动。"
        )
    if _audio_artifact_intent:
        _task_fact_parts.append(
            "The current user request asks for a user-facing audio artifact. "
            "For spoken/random test voice, use the TTS audio-generation path with a short spoken test phrase when the user did not supply text. "
            "Do not ask a TTS helper to write Python wave/noise scripts unless the user explicitly requested synthetic tones/noise instead of speech. "
            "Final deliverables should name the actual verified audio file path.\n"
            "当前请求是面向用户的音频文件产物；随机测试语音优先用 TTS 合成可听语句，最终 deliverables 写真实已验证音频文件。"
        )
    _task_facts_text = "\n\n".join(_task_fact_parts)

    # 2026-05-15 Item 3: 4 段 per-turn 动态信息一次性塞进独立 user 消息,
    # 不再 append 到 system 尾部,保 prefix cache 稳定。
    _inject_dynamic_session_info(
        base_msgs,
        pause_text=_pause_text,
        profile_text=_profile_text,
        feedback_text=_feedback_text,
        lang_directive=_lang_text,
        mode_text=_mode_text,
        project_context=_project_context_text,
        task_facts_text=_task_facts_text,
    )

    # ── 2. 分支：根据复杂度决定流程 ──
    # 安全门改为基于 Round 1 的 LLM 意图分析输出，不再做字符串匹配。
    # 字符串匹配（"图""文件""代码"）会把大量闲聊误判成 medium，浪费几十秒思考。
    # "只回复/用三个字回复/复读"这类短输出约束不是工具任务。Round1 偶尔会把它们判成
    # 任务委托并带进 Round2，导致几十秒延迟；在进入安全门前收口，避免触发 helper 链。
    if _is_direct_short_reply_request(req.message):
        if complexity != "easy" or needs_tools or needs_recall or _td.get("任务委托", 0) >= 0.7:
            debug.log(
                "round1.direct_short_reply_route",
                "explicit short reply request → force easy/no_tools/no_recall",
            )
        complexity = "easy"
        needs_tools = False
        needs_recall = False
        parallelizable = False

    # 问 OCR/TTS/LLM 等技术概念时是普通知识问答；不要因为关键词等同于工具名
    # 就进入资源规划。真正的读图、生成音频、查日志等请求由后面的意图检测处理。
    if _tool_concept_intent:
        if complexity != "easy" or needs_tools or needs_recall or _td.get("任务委托", 0) >= 0.7:
            debug.log(
                "round1.tool_concept_route",
                "tool/technology concept question → force easy/no_tools/no_recall",
            )
        complexity = "easy"
        needs_tools = False
        needs_recall = False
        parallelizable = False

    # Environment/project mode has real project files behind env_* tools. When
    # the user asks about project paths, structure, metrics, or maintenance,
    # force a tool-backed route so the answer is grounded in the real directory
    # instead of a generic architecture guess.
    #
    # bot 项目模式：涉及真实项目目录、路径、统计或维护时升级到工具链，避免无证据回答。
    if _env_project_tool_intent and not _tool_concept_intent:
        if not needs_tools or complexity == "easy":
            debug.log(
                "round1.environment_project_route",
                f"easy→medium + needs_tools=True ({_env_project_route_reason})",
            )
        needs_tools = True
        complexity = "medium" if complexity == "easy" else complexity
        if _env_project_coding_intent:
            try:
                tendency_obj.is_coding_task = True
            except Exception:
                pass

    # 任务委托 / 工具执行需求 → medium
    if complexity == "easy" and (needs_tools or _td.get("任务委托", 0) >= 0.7):
        complexity = "medium"
        debug.log("round1.safety_gate", "easy→medium (needs_tools or 任务委托)")

    # 回忆/记忆查询 → medium（否则无法展开记忆节点）
    if complexity == "easy" and needs_recall:
        complexity = "medium"
        debug.log("round1.safety_gate", "easy→medium (needs_recall)")

    # 敌意/注入测试 → medium（多一道思考避免 prompt injection）
    if complexity == "easy" and (_td.get("敌意", 0) > 0.3 or _td.get("测试", 0) > 0.7):
        complexity = "medium"
        debug.log("round1.safety_gate", "easy→medium (hostility/probing)")

    # 隐式查阅意图 → medium（lite 模型可能将"你能看到文件吗"误判为 easy 元对话）
    if complexity == "easy" and _has_implicit_recall_intent(req.message):
        complexity = "medium"
        needs_recall = True
        debug.log("round1.safety_gate", "easy→medium (implicit recall intent)")

    # 2026-05-15 P86 image safety gate
    # 病因(实测 23:06 trace "这个药是干什么的"):
    #   Round 1 lite 模型自己在 _thinking 里说"用户发来一张药品图片"、"需要理解图片内容",
    #   但居然得出 "没有 ocr 或图片描述能力" → needs_tools=False → 直接 easy 路径骗用户。
    #   实际系统装备 ocr 工具一直可用 — Round 1 提示词没明说 (已在 ROUND1_SYSTEM 模式 9 补)。
    # 修法: 服务器端兜底 — 用户消息含视觉指代词(这/看 + 药/字/图...) 或 [CQ:image] 时,
    #   即使 Round 1 说 needs_tools=False, 也强制升级到 medium + needs_tools=True。
    #   _has_image_intent_in_msg 检测视觉问询信号; 含 [CQ:image] 直接 true。
    if not needs_tools and (not _tool_concept_intent) and _has_image_intent_in_msg(req.message):
        complexity = "medium" if complexity == "easy" else complexity
        needs_tools = True
        debug.log(
            "round1.safety_gate",
            f"P86: easy→medium + needs_tools=True (image content query, "
            f"Round 1 lite 漏判: 用户问图片内容但说 needs_tools=False)",
        )

    if not needs_tools and (not _tool_concept_intent) and _audio_artifact_intent:
        complexity = "medium" if complexity == "easy" else complexity
        needs_tools = True
        debug.log(
            "round1.safety_gate",
            "easy→medium + needs_tools=True (audio artifact request; Round 1 treated it as simple reply)",
        )

    # ── 2026-05-04 Bug #4 + #11 修复:stop 命令硬路由 ──
    # Round 1 已识别意图为 元对话/中断 → Round 2 主模型仍可能"自由发挥"决定继续做活,
    # 实测 trace 2718fcc9:用户说"停,别干了" → Round 1 正确分类(元对话=0.8),
    # 但 Round 2 主模型看上下文有压缩研究历史,误以为用户在催进度,**反向 spawn 了 4 个
    # 新 helper 重做整套压缩任务**。force-kill 才能停。
    #
    # 修法:在 Round 2 入口之前做硬路由 — 短 stop 命令(≤8 字 + 含 stop 关键词)
    # 强制 complexity=easy,绝不进 Round 2,绝不创建 workspace,绝不 spawn helper。
    # 同时给 abort_channel 发 signal,让其它**正在跑的同 user 任务**(理论上不应该有,
    # per-user 锁已经串行,但保险起见)立即停。
    _msg_stripped = (req.message or "").strip()
    _looks_stop_cmd = (
        0 < len(_msg_stripped) <= 12
        and any(
            kw in _msg_stripped for kw in (
                "停", "别干", "别了", "算了", "不用了", "中断", "等等",
                "stop", "Stop", "STOP", "cancel", "Cancel",
            )
        )
    )
    _meta_high = (_td.get("元对话", 0) >= 0.6) or (_td.get("任务委托", 0) <= 0.3)
    if complexity != "easy" and _looks_stop_cmd and _meta_high:
        debug.log(
            "round1.stop_hard_route",
            f"短 stop 命令(len={len(_msg_stripped)}) + 元对话/低任务委托 → 强制 easy,"
            f"不进 Round 2,不开工作区,不 spawn helper",
        )
        complexity = "easy"
        # 不回退 needs_tools / needs_recall — easy 路径本身就不会调它们,
        # 但如果用户其实是在追问之前任务状态,easy 路径的 LLM 会自然给短答复。

    # ── 2026-05-04 Bug #12 修复:user-level stop_mode 检查 ──
    # 用户 20 秒内调过 /v1/chat/abort → 该 user 进入 stop_mode。
    # 此时即使新消息看起来不像 stop 命令(比如 "好吧" / "算了你就这样吧"),
    # 也强制走 easy 路径,不开新工作。给用户充分缓冲时间不被打扰。
    #
    # 2026-05-12 P37: 先调用 maybe_clear_stop_mode_on_repeat 检查
    # - 用户消息和上次 Jaccard >= 0.7 (E1, 2026-05-11)
    # - 或用户消息含续作意图词 "重试"/"继续"/"再来" 等 (P37 新增)
    # 实测 17:57 trace: 用户 abort 后发"重试" → 之前不清 stop_mode → round2.skip
    # → 0 helper spawn → 直接答复用户(任务完全没做)。
    # 2026-05-12 P38: stop_mode 触发时设标记, 后续 round3 注入"禁止编故事"约束
    _stop_mode_triggered = False
    try:
        _guard_check = get_group_guard()
        # 先尝试清: 续作意图 / 同消息重发
        _cleared = _guard_check.maybe_clear_stop_mode_on_repeat(
            req.archive_id, req.group_id, req.user_id, req.message or "",
        )
        if _cleared:
            debug.log(
                "round1.stop_mode_cleared",
                "P37: 用户消息含续作意图(或与上次相似), 清 stop_mode 走正常路径",
            )
        # 记录本次消息(供下次比对)
        try:
            _guard_check.record_user_message(
                req.archive_id, req.group_id, req.user_id, req.message or "",
            )
        except Exception:
            pass
        if complexity != "easy" and _guard_check.is_in_stop_mode(
            req.archive_id, req.group_id, req.user_id,
        ):
            # 2026-05-21: 这条消息是否已被 Round2.5 abort 总结合并回答过?
            # 是 → 它只是\"打断时追加的新询问\"在 /interrupt_message 与 /chat 两条路径
            # 下的重复投递, 已经在 abort 总结里一并答过; 此处直接跳过, 不再发第二条
            # 强制 easy 回复(修复实测 trace 13cedd4e 10:38 的双回复)。
            # 注意: 只读状态查询(进度/状态)即便重复也无害, 但既然已合并答过, 同样跳过。
            _readonly_status_query = any(
                kw in (req.message or "").lower()
                for kw in (
                    "进度", "状态", "到哪", "做到哪", "怎么样", "检查", "查看",
                    "status", "progress", "what happened", "where are we",
                )
            )
            if _guard_check.consume_abort_injected(
                req.archive_id, req.group_id, req.user_id, req.message or "",
            ):
                debug.log(
                    "round1.stop_mode_injected_skip",
                    "user 打断时的新询问已在 Round2.5 abort 总结中合并回答 → 跳过本轮,"
                    "避免双回复",
                )
                yield "complete", {"trace_id": trace_id, "skipped": "abort_injected_duplicate"}
                return
            if _readonly_status_query:
                debug.log(
                    "round1.stop_mode_readonly_allowed",
                    "user 在 abort 后询问进度/状态 → 保留工具链做只读核验,不开新写作任务",
                )
                _stop_mode_triggered = True
                needs_tools = True
                needs_recall = True
            else:
                debug.log(
                    "round1.stop_mode_active",
                    "user 在 abort 后的 stop_mode 窗口内 → 强制 easy(不开新工作)",
                )
                complexity = "easy"
                _stop_mode_triggered = True  # P38: round3 据此注入诚实约束
    except Exception:
        # stop_mode 查询失败不影响主流程
        pass

    # 工作区:仅在走 Round 2 时创建（medium/hard），easy 路径不需要
    workspace_dir = ""           # 临时工作区路径(.temp/) — Round 2/helper 实际工作目录
    main_workspace_dir = ""      # 主工作区路径(干净区) — 仅装核心成果
    group_key = f"{req.archive_id}:{req.group_id}"
    generated_files: list[tuple[str, str, str]] = []  # (filename, url, local_path)
    _files_before: set[str] = set()  # Round 2 前已存在的文件（仅追踪本轮新增）

    if complexity != "easy":
        # 2026-05-03 Bug E 修:双层工作区
        # main_workspace = 持久化的核心成果(干净)
        # workspace_dir(=.temp/)= 当前对话的所有 Round 2 / helper 工作
        # ensure_temp_workspace 把主区当前内容 sync 到 .temp(增量,保留 _delegate_*)
        main_workspace_dir = ws_tool.create_workspace(
            archive_id=req.archive_id, group_id=req.group_id
        )
        # ── Opt 3: 归档超过 14 天的旧制品文件 ──
        ws_tool.archive_stale_artifacts(main_workspace_dir, max_age_days=14)
        # 2026-05-07 Bug 4 fix: use trace_id as session_tag for cross-task _shared/ detection
        _session_tag = f"{req.archive_id}:{req.group_id}:{req.user_id}:{hash(req.message) & 0xFFFFFFFF:08x}"
        workspace_dir = ws_tool.ensure_temp_workspace(main_workspace_dir, session_tag=_session_tag)
        _cap = ws_tool.enforce_workspace_capacity(
            workspace_dir,
            label="main_temp_workspace",
        )
        if not _cap.get("ok", True):
            debug.log(
                "workspace.capacity.blocked",
                "当前临时工作区超过单智能体容量上限,安全整理后仍过大; 本轮不启动工具线程",
                _cap,
            )
            yield "error", {"message": "Current workspace exceeds the capacity limit after cleanup; please organize the workspace before retrying.\n当前工作区超过容量上限。"}
            return
        ws_tool.register_workspace(group_key, workspace_dir)
        debug.log(
            "workspace.layered",
            f"main={main_workspace_dir} temp={workspace_dir}",
        )
        # ── 选择性清理:删跨用户 _delegate_*,保留当前用户的续作目录 ──
        _cleanup_cross_user_delegate_dirs(workspace_dir, req.user_id, debug=debug)
        # ── 清理超过 7 天的旧 _delegate_* 目录(同一用户,续作已无价值) ──
        _cleanup_old_same_user_delegate_dirs(workspace_dir, req.user_id, max_age_days=7, debug=debug)
        # ── 容量治理: 保留当前用户最近少量非活跃 helper, 回收更旧的已死/已完成 helper 沙箱 ──
        _cleanup_inactive_delegate_dirs(workspace_dir, req.user_id, max_keep=8, debug=debug)
        # ── 2026-05-15 P64: 清理 _helpers_shared/ 过期子目录 ──
        # 病因(实测 16:28 comp_custom): 上一轮 sorting 任务的 radix_bench/*.c/h 残留, 被
        # 误算成本任务 helper 的"已交付到主区"产物 → 主线程被误导。
        _cleanup_stale_helpers_shared(workspace_dir, max_age_days=3, keep_recent=10, debug=debug)
        # ── 清理跨对话污染:删旧的 .helper_*_summary.txt,防止历史 helper 残留误触发 ──
        _stale_summaries = (
            glob.glob(os.path.join(workspace_dir, ".helper_*_summary.txt"))
            + glob.glob(os.path.join(main_workspace_dir, ".helper_*_summary.txt"))
        )
        for _ss in _stale_summaries:
            try:
                os.remove(_ss)
            except OSError:
                pass
        if _stale_summaries:
            debug.log(
                "workspace.cleanup_stale_summaries",
                f"removed {len(_stale_summaries)} stale .helper_*_summary.txt"
                " files from previous conversations",
            )
        _files_before = set(ws_tool.list_generated_files(workspace_dir))

        # ── Opt 2: 写入会话文件清单,供 final_auto helper 做污染鉴别 ──
        _manifest_path = os.path.join(workspace_dir, "_session_manifest.json")
        try:
            _manifest = {
                "session_start": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                "user_id": req.user_id,
                "files_before": sorted(_files_before),
                "hint": (
                    "files_before lists files that already existed at the start of this round. "
                    "New deliverables are not in this list. Final/verify helpers should prefer newly created "
                    "files when judging this round's outputs.\n"
                    "files_before 表示本轮开始前已有文件，新产物不在其中。"
                ),
            }
            with open(_manifest_path, "w", encoding="utf-8") as _mf:
                json.dump(_manifest, _mf, ensure_ascii=False)
            debug.log(
                "workspace.manifest",
                f"_session_manifest.json written with {len(_files_before)} pre-existing files",
            )
        except OSError:
            pass

    try:
        # ── 获取 per-user abort 通道 ──
        # 2026-05-01 改造:per-group → per-user 锁,abort 通道也 per-user 隔离。
        # 用 generation 号代替单一 Event,杜绝 Bug 27 的窗口期：
        #   - Round 2 期间的 abort 信号只对 Round 2 生效
        #   - Round 3 启动前 snapshot 一次 gen,只对 gen 之后的新信号响应
        guard = get_group_guard()
        abort_ch = guard.get_abort_channel(req.archive_id, req.group_id, req.user_id)
        abort_event = abort_ch.event   # 兼容老接口（_round2/_round3 仍读 event）
        progress_log: list[str] = []  # 收集进度消息供维护阶段写入热记忆
        _intermediate_feedback_pref = 0.5
        try:
            from app.memory.persona_files import persona_intermediate_feedback_preference_by_content
            _intermediate_feedback_pref = persona_intermediate_feedback_preference_by_content(
                persona or "",
                0.5,
            )
        except Exception:
            _intermediate_feedback_pref = 0.5
        try:
            from app.core.runtime_mode import is_environment_mode
            _intermediate_feedback_channel = "agent" if is_environment_mode() else "chat"
        except Exception:
            _intermediate_feedback_channel = "agent" if (req.current_dir or "").strip() else "chat"
        from app.core.intermediate_feedback import IntermediateFeedbackGate
        _intermediate_feedback_gate = IntermediateFeedbackGate(
            preference=_intermediate_feedback_pref,
            channel=_intermediate_feedback_channel,
        )
        debug.log(
            "intermediate.preference",
            f"value={_intermediate_feedback_pref:.2f} channel={_intermediate_feedback_channel}",
        )
        guard.set_stage(req.archive_id, req.group_id, req.user_id, "round1")

        # 进 Round 2 前 snapshot,用于稍后判断"R2 期间是否被 abort 过"。
        _round_start_gen = abort_ch.gen

        # ── Patch 04 防御性默认 plan ──
        # 任何分支都不应该让 plan 走到 deliverables 处理时未定义。
        # 异常路径(_drive_round2 task cancel/异常未产出 _plan 事件)会用这个兜底,
        # Round 3 仍能基于用户消息生成回复,而不是 NameError。
        plan = _fallback_plan_from_user(req.message, "未走完正常路径")

        # ── 2026-05-02 part9 重构:round2 4 路分支用 stage 表统一驱动 ──
        # 之前 4 路(easy/hard/medium-coding/medium-non-coding)+ 各自的升级链
        # 总共 ~210 行重复:都是同样的 yield progress + section + _drive_round2 + log
        # 4 路差异仅在 reasoning / lite / helper_lite / max_iter 几个标量。
        # 现在抽 stage table,主体只剩"决定 stage 序列 → 顺序跑"。
        # 升级是"上一档跑完看 plan.upgrade_to_*  → 决定是否跑下一档",从原来 4 处
        # 重复代码合并成一处显式的 while loop。
        # 2026-05-02 part10 P5:helper_excerpts 累积容器,
        # 跨 stage 升级时合并(后档可能 spawn 新 helper,前档已有的也保留)。
        all_helper_excerpts: dict[str, str] = {}
        # 2026-05-06: verification_needed 跨 stage 累积
        all_verification_needed: bool = False
        all_verification_advice: str = ""
        # 2026-05-15 P84: 主线程"数据型"工具结果 (ocr/inspect_file) 复用 all_helper_excerpts
        # 通道传给 Round 3, key 用 'ocr#1' 这种与 helper task_id 不冲突的命名。
        if complexity == "easy":
            guard.set_stage(req.archive_id, req.group_id, req.user_id, "round2")
            debug.log("round2.skip", "问题简单，跳过规划直接回复")
            # 2026-05-02 Bug M 修:用工厂函数拿新实例,防止下游对 plan.avoid /
            # plan.key_points 的 mutate 污染模块级 _EASY_PLAN 常量。
            plan = _make_easy_plan()
            # 2026-05-12 P38: stop_mode 触发时, 强制 plan 含"诚实告知"指令
            # 病因(实测 17:57 trace): 用户 abort 后发"重试" → P37 还没部署被卡 stop_mode
            # → 强制 easy → round3 lite 模型看见对话历史上次任务有 paper.docx,
            # 编出 "又试了一次, benchmark 重新跑完了, 六张图和 paper.docx 都重新生成了" —
            # 完全撒谎, 实际什么都没做!
            # 修法: stop_mode 触发时, 在 plan.key_points / avoid 注入硬约束。
            if _stop_mode_triggered:
                plan.avoid = (list(plan.avoid) if plan.avoid else []) + [
                    "Keep interruption honesty: this turn did not start new work, so the reply must not claim completion, reruns, generated files, or renewed execution.\n中断后保持诚实，不声称已完成、重跑或新生成文件。",
                    "Treat prior-turn artifacts as history unless this turn verified or re-delivered them.\n历史产物只作历史，不当成本轮结果。",
                ]
                plan.key_points = (list(plan.key_points) if plan.key_points else []) + [
                    "The previous task was interrupted and this cooldown turn starts no new work.\n上次任务已中断，本轮未开启新工作。",
                    "Reply briefly: state that work is stopped and not redone; ask for one clear instruction if the user wants a new run.\n短句说明已中止且未重做，需要继续时请给明确指令。",
                    "Honesty is the highest priority; progress claims require evidence from this turn.\n诚实优先，进度声明需要本轮证据。",
                ]
                debug.log(
                    "round3.stop_mode_plan",
                    "P38: 注入诚实约束到 easy plan (avoid + key_points)",
                )
            if _tool_concept_intent:
                plan.key_points = (list(plan.key_points) if plan.key_points else []) + [
                    "The user is asking a tool or technical concept question; explain the concept itself and keep it separate from execution.\n工具概念问题只解释概念，不转入实操。",
                    "Offer practical execution only when the user explicitly asks for it.\n只有用户明确要求时才进入实操。",
                ]
                plan.avoid = (list(plan.avoid) if plan.avoid else []) + [
                    "Keep the reply as a concept explanation rather than a tool-invocation invitation.\n回复保持概念解释，不变成工具调用邀请。",
                    "Describe no current or upcoming tool execution unless the plan has evidence for it.\n没有证据时不暗示本轮已调用或即将调用工具。",
                ]
                debug.log(
                    "round3.tool_concept_plan",
                    "concept-only turn: keep Round3 focused on explanation, not tool invitation",
                )
            await debug.report()
        else:
            # 选起始 stage:
            #   hard           → "hard"(main + thinking=disabled,可升 veryhard)
            #   medium + 编码  → "medium_coding"(main 模型 + main helper,可升 veryhard)
            #   medium + 非编码 → "medium"(lite + lite helper,可升 hard 再升 veryhard)
            if complexity == "hard":
                yield "thinking", {"text": "让我想想..."}
                debug.log("round2.thinking", "hard question — sent thinking reply")
                start_stage = "hard"
            elif bool(getattr(tendency_obj, "is_coding_task", False)) or \
                 bool(getattr(tendency_obj, "is_document_task", False)):
                # 编码/文档任务直接走 main 模型(不用 lite)。
                # 编码: lite 写 C/Python 反复修不到根上,把 1.1M token 全消耗在重复编辑上。
                # 文档: lite 生成 office block 常缺图/漏表/排版乱,main 能一次写对。
                # 同样 helper_lite=False — 编码/文档 helper 用 lite 是负价值。
                # 2026-05-10 Patch 82 设计原则:Round 1 不做"人设是否拒绝"决策。
                # Round 1 是分类(complexity / needs_tools / is_coding_task 等),
                # 应该尽可能简单。任何可能拒绝/接受的判断都交给 Round 2 主线程,
                # 因为 Round 2 才看得到完整人设 + 上下文 + 防注入攻击。
                # 所以这里**仍然进 medium_coding**,让 main 路径的 LLM 在 plan 阶段
                # 按人设决策(P82 v2 强化的 P76 注入会让 LLM 真正遵守人设)。
                debug.log(
                    "round2.direct_main",
                    "is_coding_task or is_document_task → bypassing lite, "
                    "using main from start (both main thread and helpers)",
                )
                start_stage = "medium_coding"
            else:
                start_stage = "medium"

            current_stage = start_stage
            prior_plan: ResponsePlan | None = None
            guard.set_stage(req.archive_id, req.group_id, req.user_id, "round2")
            if _stop_mode_triggered:
                base_msgs.append({
                    "role": "system",
                    "content": (
                        "## Read-only status check after interruption\n"
                        "The user recently interrupted the task and is now asking for progress or status. "
                        "Use only read-only status and inspection tools: delegate(action='status'/'poll'/'collect' "
                        "with a short wait_window_sec), processes(action='list'), and workspace list/read/inspect "
                        "operations. Use delegate(action='status') for overall helper state. Report only verified "
                        "facts: what is running, what completed, what is missing, and the recommended next step. "
                        "Continue production work only when the user explicitly asks to resume.\n\n"
                        "中断后的状态询问只做只读核验，说明运行中、已完成、缺失和建议下一步。"
                    ),
                })
            while current_stage is not None:
                # 该 stage 的 ROUND 2 段头(用于 debug 日志可读)
                debug.section(_R2_STAGE_TABLE[current_stage]["section_title"])
                stage_progress_event = _R2_STAGE_TABLE[current_stage]["progress_event"]
                yield "progress", stage_progress_event
                _milestone_reply = await maybe_generate_milestone_feedback(
                    payload={
                        **stage_progress_event,
                        "kind": "main_milestone",
                        "milestone": f"round2_stage_{current_stage}_started",
                    },
                    gate=_intermediate_feedback_gate,
                    persona=persona,
                    user_message_text=req.message,
                    progress_log=progress_log,
                    stage="round2",
                )
                if _milestone_reply:
                    yield "intermediate_reply", _milestone_reply

                stage_cfg = _R2_STAGE_TABLE[current_stage]
                from app.llm.model_pool import TASK_TIER
                stage_think, stage_tier = TASK_TIER[stage_cfg["task_name"]]
                async for ev_type, ev_data in _drive_round2(
                    base_msgs, tendency_obj,
                    archive_id=req.archive_id, group_id=req.group_id,
                    user_id=req.user_id, workspace_dir=workspace_dir,
                    abort_event=abort_event, progress_log=progress_log,
                    intermediate_feedback_gate=_intermediate_feedback_gate,
                    think=stage_think, persona=persona,
                    tier=stage_tier, helper_lite=stage_cfg["helper_lite"],
                    parallelizable=parallelizable,
                    needs_tools=needs_tools,
                    needs_recall=needs_recall,
                    inline_images=inline_images,
                    max_iter=stage_cfg["max_iter"],
                    prior_plan=prior_plan,
                    user_message_text=req.message,  # 2026-05-03:供 recall_thread
                ):
                    if ev_type == "_plan":
                        plan = ev_data["plan"]
                        _apply_user_output_constraints(plan, req.message)
                    elif ev_type == "intermediate_reply":
                        yield ev_type, ev_data
                    elif ev_type == "_helper_excerpts":
                        # 2026-05-02 part10 P5:本 stage 收集到的 helper 报告 excerpt,
                        # merge 进总 dict(后 stage 同 task_id 覆盖前 stage,因为 helper 是续作)
                        excerpts = ev_data.get("excerpts") or {}
                        if isinstance(excerpts, dict):
                            all_helper_excerpts.update(excerpts)
                        # 2026-05-06: 跨 stage 累积 verification_needed
                        if ev_data.get("verification_needed"):
                            all_verification_needed = True
                            _va = ev_data.get("verification_advice", "")
                            if _va:
                                all_verification_advice = _va
                    elif ev_type == "_main_tool_results":
                        # 2026-05-15 P84: 主线程工具(ocr/inspect_file)结果, 给 Round 3 看。
                        # 严重 bug 修复(实测 价目表 trace): Round 2 plan key_points 仅做元描述
                        # 不含 OCR 原文, Round 3 凭空编造内容。把原始工具结果作为额外条目加
                        # 到 all_helper_excerpts, Round 3 看到原始文本就不会编。
                        _tool_results = ev_data.get("results") or {}
                        if isinstance(_tool_results, dict):
                            for _k, _v in _tool_results.items():
                                if _v and isinstance(_v, str):
                                    all_helper_excerpts[_k] = _v
                    else:
                        yield ev_type, ev_data

                debug.log(stage_cfg["log_event"], f"intent={plan.intent}", plan.model_dump())
                plan = _choose_non_regressed_plan(prior_plan, plan)

                # 决定下一档:看 plan.upgrade_to_* + 不在 abort 状态
                next_stage = _next_r2_stage(current_stage, plan, abort_event.is_set())
                if next_stage is None:
                    break
                # 升级日志 + 进度事件
                log.info("[%s] round2 upgrade: %s→%s", trace_id, current_stage, next_stage)
                debug.log(
                    f"round2.upgrade_{next_stage}",
                    f"upgrading from {current_stage} to {next_stage}",
                )
                yield "progress", _progress_payload(
                    "planning_tools",
                    "planning",
                    "正在加强核验并调整方案",
                    workflow_adjustment="evidence_recheck",
                )
                _upgrade_reply = await maybe_generate_milestone_feedback(
                    payload={
                        "kind": "main_milestone",
                        "milestone": f"round2_upgrade_{next_stage}",
                        "message": "The workflow is rechecking evidence and tightening the plan.\n正在核验证据并收紧方案。",
                    },
                    gate=_intermediate_feedback_gate,
                    persona=persona,
                    user_message_text=req.message,
                    progress_log=progress_log,
                    stage="round2",
                )
                if _upgrade_reply:
                    yield "intermediate_reply", _upgrade_reply
                prior_plan = plan
                current_stage = next_stage

            log.info("[%s] round2 intent=%s", trace_id, plan.intent)
            debug.log("round2.done", "plan", plan.model_dump())
            _round2_done_reply = await maybe_generate_milestone_feedback(
                payload={
                    "kind": "main_milestone",
                    "milestone": "round2_done",
                    "message": (
                        f"Planning and tool work reached a handoff point with intent={getattr(plan, 'intent', '')}.\n"
                        "规划和工具阶段已到达交接点。"
                    ),
                },
                gate=_intermediate_feedback_gate,
                persona=persona,
                user_message_text=req.message,
                progress_log=progress_log,
                stage="round2",
            )
            if _round2_done_reply:
                yield "intermediate_reply", _round2_done_reply
            await debug.report()
        _mark("round2_done")

        # 2026-05-02 part10 (P7):needs_recall 事后审计(仅观察 log,不阻塞)。
        # round1 判 needs_recall=true 时,base_msgs system 段含完整记忆索引,但 round2
        # 模型可能完全没读 system 直接答(实测 trace 见过)。这里事后看模型是否真
        # 用了记忆——调过 expand_*/search_files,或 plan.key_points 引用了 system 中
        # 的具体内容(实体名、文件名)。没用算 "recall_unused",积累数据为下次 prompt
        # 改动提供依据。失败兜底,不影响主流程。
        if needs_recall and complexity != "easy":
            try:
                _recall_audit_recall_used(
                    plan, base_msgs, debug=debug, trace_id=trace_id,
                )
            except Exception:
                pass  # 纯观察工具,失败静默

        # 扫描工作区中 AI 本轮新生成的文件，只推送 plan.deliverables 中列出的
        promoted_to_main_total: list[str] = []  # 累积自 promote_to_main,bot_log 用
        if workspace_dir:
            # 兜底：模型常常忘了把交付物列入 plan.deliverables，导致前端收不到文件。
            # 实测 case：用户说「帮我修代码」→ 模型修好 hello_fixed.c 编译验证通过 →
            # plan.deliverables=[]（误以为 key_points/Round3 贴代码就够了）→ 用户没收到文件。
            # 这里在 needs_tools=True 且 deliverables 空时按启发式补充。
            try:
                from app.core.runtime_mode import is_environment_mode as _is_environment_mode
                _env_mode_for_deliverables = bool(_is_environment_mode())
            except Exception:
                _env_mode_for_deliverables = False
            if _env_mode_for_deliverables:
                debug.log(
                    "round2.deliverables.autofix.skip",
                    "environment mode uses project files as outputs; skip chat attachment autofix",
                )
            else:
                _autofix_deliverables(
                    plan,
                    user_message=req.message,
                    needs_tools=needs_tools,
                    workspace_dir=workspace_dir,
                    files_before=_files_before,
                )

            # 2026-05-09 BUG FIX (trace 96c47298): autofix 看到工作区有 N 个产物 +
            # plan 是 fallback 状态(intent 含"降级"/internal_note 含"fallback") →
            # 改写 plan 让 Round 3 知道"实际有产出,只是 summarizer 失败了"。
            # 旧行为:Round 3 拿到 fallback intent="降级:JSON 重写失败" + key_points 占位
            # + 8 个 deliverables → 不知所措 → 生成 81 字"还没好你等等"骗用户。
            # 实际上 helper 干的活儿是真有产物的,只是主线程总结那一步崩了。
            # 新行为:在 plan 已被 autofix 填进文件名后,若 plan 看起来还是 fallback
            # 形态,把 intent/key_points 改成"实事求是承认"模板:
            #   - 列出生成的文件(让 Round 3 用人设语言提及)
            #   - 明示"汇总环节崩了,但产物在工作区里"
            #   - avoid 列入"装作什么都没发生"
            # Round 3 仍走人设流式,但有了正确的事实素材,不会再说"等会儿才好"。
            # 2026-05-09 收紧:用具体标记短语,避免 "fallback" 在正常 plan 中误触发。
            _internal_note_lc = (plan.internal_note or "").lower()
            _is_fallback_plan = (
                ("降级:" in plan.intent)                       # 非系统错误兜底
                or ("fallback plan 触发" in plan.internal_note)  # 系统错误 + 非系统错误都加这个
                or ("fallback plan" in _internal_note_lc and "触发" in plan.internal_note)
                or ("（用户消息为空）" in plan.key_points)         # 旧路径兼容
                or ("承认刚才内部出错了" in plan.intent)            # 系统错误 plan 的 intent 特征
            )
            if _is_fallback_plan and plan.deliverables:
                # 取前 6 个文件名 basename,避免太长
                _names = []
                for f in plan.deliverables[:6]:
                    nm = (f or "").replace("\\", "/").split("/")[-1]
                    if nm:
                        _names.append(nm)
                _files_str = ", ".join(_names)
                if len(plan.deliverables) > 6:
                    _files_str += f" ... +{len(plan.deliverables) - 6}"

                _orig_user = (req.message or "").strip()[:120]
                plan.intent = (
                    "Reply briefly in persona. Tell the user that the requested work is mostly complete and the listed files are available, "
                    "while the final summary step failed. Keep the wording honest: the files exist, so do not describe the work as still waiting or not done. "
                    "Point the user to the delivered files for details.\n"
                    "用人设短句说明产物已在，最终汇总失败；不要说还没做完。"
                )
                plan.key_points = [
                    f"Delivered files: {_files_str}\n产物文件: {_files_str}",
                    "The files were delivered into the conversation and can be viewed or downloaded.\n文件已推送到对话中，可查看或下载。",
                    "The final narrative summary failed, but the artifacts themselves are real.\n汇总文字失败，产物本身存在。",
                ]
                if _orig_user:
                    plan.key_points.append(f"Original user request excerpt: {_orig_user!r}\n用户原话摘录。")
                plan.avoid = list(plan.avoid or []) + [
                    "Use completion-state wording grounded in existing artifacts; do not imply the files are still pending.\n产物已存在时用完成态措辞。",
                    "Keep the final message short because the summary stage failed.\n汇总失败时短句说明。",
                    "Acknowledge the summary problem plainly instead of presenting a fully normal completion narrative.\n坦诚说明汇总环节出错。",
                ]
                plan.tone = "坦诚、人设语气、简短"
                plan.length_hint = "短"
                plan.internal_note = (
                    f"⚠ autofix 修补:原 plan 是 fallback,但工作区有 "
                    f"{len(plan.deliverables)} 个产物。改写 plan 让 Round 3 诚实说"
                    f"'活干了大半,汇总崩了'。原 internal_note: "
                    f"{(plan.internal_note or '')[:80]}"
                )[:300]
                debug.log(
                    "round2.fallback_plan_rewritten",
                    f"fallback plan + {len(plan.deliverables)} deliverables → "
                    f"rewritten intent for honest Round 3",
                    {"deliverables": plan.deliverables, "files_str": _files_str},
                )

            # 注:之前这里有个 register_deliverables() 调用,用于让 .exe 类危险扩展名
            # 在 plan.deliverables 列出时可下载。后来的设计修订:可执行文件一律拒绝下载
            # (无论是否在 deliverables 里),因为"询问/警告"机制对真实威胁无效但伤害人设。
            # 见 app/api/chat.py:_BLOCKED_EXTENSIONS。

            want = set(plan.deliverables) if plan.deliverables else set()
            redeliver_existing = bool(want) and not any(f not in _files_before for f in want)
            if redeliver_existing:
                debug.log(
                    "workspace.redeliver.intent",
                    "plan.deliverables 全部是本轮开始前已存在的文件，按模型显式决定重推",
                    sorted(want),
                )
            if want:
                _temp_files_now = set(ws_tool.list_generated_files(workspace_dir))
                _missing_in_temp = want - _temp_files_now
                if _missing_in_temp and main_workspace_dir:
                    try:
                        _main_files_now = set(ws_tool.list_generated_files(main_workspace_dir))
                    except Exception:
                        _main_files_now = set()
                    _fetched_from_main: list[str] = []
                    for _fname in sorted(_missing_in_temp & _main_files_now):
                        _src = os.path.join(main_workspace_dir, _fname)
                        _dst = os.path.join(workspace_dir, _fname)
                        try:
                            if os.path.isfile(_src):
                                _dst_dir = os.path.dirname(_dst)
                                if _dst_dir:
                                    os.makedirs(_dst_dir, exist_ok=True)
                                shutil.copy2(_src, _dst)
                                _fetched_from_main.append(_fname)
                        except OSError:
                            pass
                    if _fetched_from_main:
                        debug.log(
                            "workspace.deliverable.fetch_main",
                            "模型显式列入 deliverables 的文件只在主区存在，已复制到 .temp 以便推送",
                            _fetched_from_main,
                        )

            _deliverable_warning_facts: list[str] = []
            if want:
                _preexisting_artifacts = {
                    f for f in want
                    if f in _files_before
                    or f.replace("\\", "/") in {str(x).replace("\\", "/") for x in _files_before}
                }
                if _preexisting_artifacts and not redeliver_existing:
                    _kind = "audio " if _is_new_audio_file_request(req.message or "") else ""
                    _deliverable_warning_facts.append(
                        f"Delivery fact: the plan selected {_kind}file(s) that already existed before this round while other selected deliverable(s) are new: "
                        + ", ".join(sorted(_preexisting_artifacts))
                        + ". These files remain selected for model review; decide whether each is a final artifact for the current request or old/source/context material."
                    )
                    debug.log(
                        "workspace.preexisting_artifact_flagged",
                        "explicit deliverable(s) already existed before the round; kept and recorded fact",
                        sorted(_preexisting_artifacts),
                    )

            # Push-layer boundary facts: do not remove explicit deliverables here.
            # 病因(trace 6c60898a160b4f6c):主线程 abort 路径从 helper delegate output
            # 抠 deliverables 时,把 .helper_xxx_full_report.txt(内部摘要)放进 plan.deliverables。
            # Earlier code removed such files symbolically. Current policy: if the LLM
            # explicitly selected an existing file, record the boundary warning as a fact
            # for the final model response; physical/safety checks below still apply.
            _internal_in_want = {
                f for f in want
                if _is_internal_deliverable_file(f)
            }
            if _internal_in_want:
                _deliverable_warning_facts.append(
                    "Delivery fact: selected deliverable(s) match internal/staging naming boundaries: "
                    + ", ".join(sorted(_internal_in_want))
                    + ". They remain selected because plan.deliverables is an explicit model decision; decide wording from the file facts."
                )
                debug.log(
                    "workspace.internal_flagged",
                    f"{len(_internal_in_want)} explicit deliverable(s) match internal/staging boundaries; kept and recorded fact",
                    {"flagged": sorted(_internal_in_want)},
                )
            if _deliverable_warning_facts:
                existing_points = list(plan.key_points or [])
                for _fact in _deliverable_warning_facts:
                    if _fact not in existing_points:
                        existing_points.append(_fact)
                plan.key_points = existing_points
                _note = (plan.internal_note or "").strip()
                _warn_note = "deliverable boundary facts recorded for final response"
                plan.internal_note = ((_note + " | " if _note else "") + _warn_note)[:300]
                await _review_explicit_deliverables_with_warnings(
                    plan,
                    user_message=req.message,
                    workspace_dir=workspace_dir,
                    files_before=_files_before,
                    warning_facts=_deliverable_warning_facts,
                )
                want = set(plan.deliverables) if plan.deliverables else set()
                redeliver_existing = bool(want) and not any(f not in _files_before for f in want)

            _skipped_non_deliverable: list[str] = []
            for fname in ws_tool.list_generated_files(workspace_dir):
                if fname not in want:
                    _skipped_non_deliverable.append(fname)
                    continue
                full_path = os.path.join(workspace_dir, fname)
                # 2026-05-06 §C6.3: 零字节文件绝不交付
                try:
                    if os.path.getsize(full_path) == 0:
                        log.warning("deliverable %s is zero bytes, skipping", fname)
                        continue
                except OSError:
                    continue
                if fname in _files_before:
                    # 2026-05-17: plan.deliverables 是 LLM 对本轮要推送文件的显式决定。
                    # 若本轮没有新产物、全是已存在文件, 这是“重推/补推”场景,不能按污染残留跳过。
                    if redeliver_existing:
                        debug.log("workspace.redeliver", f"re-delivering existing file selected by plan: {fname}")
                    else:
                        # 产物类文件若在会话开始前已存在,这是交付事实而不是推送层
                        # 可以替 LLM 做的语义判断。上方已把 pre-existing fact 注入
                        # plan 并交给模型复核；此处只负责物理/安全边界。
                        ext = os.path.splitext(fname)[1].lower()
                        _STALE_ARTIFACT_EXTS = (
                            ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp",
                            ".docx", ".pptx", ".xlsx", ".pdf", ".zip", ".csv",
                        )
                        if ext in _STALE_ARTIFACT_EXTS:
                            debug.log(
                                "workspace.preexisting_artifact.deliver",
                                f"delivering explicit pre-existing artifact after model-visible fact review: {fname}",
                            )
                        else:
                            debug.log("workspace.redeliver", f"re-delivering modified file: {fname}")
                url = f"/v1/chat/files/{req.archive_id}/{req.group_id}/{fname}"
                generated_files.append((fname, url, full_path))
            if _skipped_non_deliverable:
                debug.log(
                    "workspace.skip_summary",
                    f"{len(_skipped_non_deliverable)} non-deliverable files skipped; "
                    f"first 5: {_skipped_non_deliverable[:5]}",
                )
            if generated_files:
                debug.log("workspace.files", f"{len(generated_files)} delivered", generated_files)
            if want:
                delivered_names = {f for f, _, _ in generated_files}
                missing = want - delivered_names
                # 2026-05-10 Patch 57: 前缀回退匹配
                # 2026-05-11 Patch 58: 多候选时取最新修改的(实测 trace 21:26:59:
                #   主进程找 chart1_overview.png, 工作区有 gen_paper_chart1_overview.png
                #   + gen_charts_chart1_overview.png 两个候选 → P57 require len==1 不匹配
                #   → P56 误判 missing → 主进程跟用户说"图没生成", 但实际图已嵌入论文)
                if missing:
                    _all_ws = ws_tool.list_generated_files(workspace_dir)
                    _resolved: dict[str, str] = {}
                    _ambiguous_resolve_facts: list[str] = []
                    for _m in sorted(missing):
                        _suffix = "_" + _m
                        _candidates = [
                            f for f in _all_ws
                            if f.endswith(_suffix) and f not in delivered_names
                        ]
                        _fresh_candidates = [f for f in _candidates if f not in _files_before]
                        if _fresh_candidates or not redeliver_existing:
                            _candidates = _fresh_candidates
                        if len(_candidates) == 1:
                            _resolved[_m] = _candidates[0]
                        elif len(_candidates) > 1:
                            _ambiguous_resolve_facts.append(
                                f"{_m}: multiple prefixed candidates exist; no automatic choice was made: "
                                + ", ".join(sorted(_candidates)[:8])
                            )
                            debug.log(
                                "workspace.prefix_resolve.ambiguous",
                                f"'{_m}' has {len(_candidates)} fresh prefixed candidates; leaving unresolved",
                                sorted(_candidates)[:20],
                            )
                        elif not _candidates:
                            _preexisting_candidates = [
                                f for f in _all_ws
                                if f.endswith(_suffix) and f not in delivered_names and f in _files_before
                            ]
                            if _preexisting_candidates and not redeliver_existing:
                                _ambiguous_resolve_facts.append(
                                    f"{_m}: only pre-existing prefixed candidate(s) were found; no automatic re-delivery was made: "
                                    + ", ".join(sorted(_preexisting_candidates)[:8])
                                )
                                debug.log(
                                    "workspace.prefix_resolve.preexisting_only",
                                    f"'{_m}' only matched pre-existing prefixed candidate(s); leaving unresolved",
                                    sorted(_preexisting_candidates)[:20],
                                )
                    if _ambiguous_resolve_facts:
                        plan.key_points = list(plan.key_points or []) + [
                            "Delivery fact: prefix-based deliverable resolution was ambiguous or only matched pre-existing artifacts. "
                            + " | ".join(_ambiguous_resolve_facts)
                        ]
                    if _resolved:
                        _STALE_EXTS = (
                            ".png", ".jpg", ".jpeg", ".gif", ".svg",
                            ".webp", ".bmp", ".docx", ".pptx", ".xlsx",
                            ".pdf", ".zip", ".csv",
                        )
                        for _orig, _actual in _resolved.items():
                            _fp = os.path.join(workspace_dir, _actual)
                            try:
                                if os.path.getsize(_fp) == 0:
                                    continue
                            except OSError:
                                continue
                            if _actual in _files_before:
                                _ext = os.path.splitext(_actual)[1].lower()
                                if _ext in _STALE_EXTS and not redeliver_existing:
                                    debug.log(
                                        "workspace.prefix_resolve.preexisting",
                                        f"resolved explicit deliverable to pre-existing artifact fact, still delivering: {_actual}",
                                    )
                            _url = f"/v1/chat/files/{req.archive_id}/{req.group_id}/{_actual}"
                            generated_files.append((_actual, _url, _fp))
                        debug.log(
                            "workspace.prefix_resolve",
                            f"P57: resolved {len(_resolved)} missing deliverable(s)",
                            {orig: actual for orig, actual in _resolved.items()},
                        )
                        delivered_names = {f for f, _, _ in generated_files}
                        # 2026-05-22 修复:resolve 成功的文件以带前缀真名进 generated_files,
                        # want 里仍是无前缀原名；否则 resolve 成功后还会被误判 missing。
                        _resolved_origs = set(_resolved.keys())
                        missing = (want - delivered_names) - _resolved_origs
                if missing:
                    _env_project_files = _existing_environment_project_files(missing)
                    if _env_project_files:
                        missing -= _env_project_files
                        if plan.deliverables:
                            plan.deliverables = [
                                d for d in plan.deliverables if d not in _env_project_files
                            ]
                        _env_lines = ", ".join(sorted(_env_project_files))
                        if not any(
                            "项目目录文件" in str(kp) and _env_lines in str(kp)
                            for kp in (plan.key_points or [])
                        ):
                            plan.key_points = list(plan.key_points or []) + [
                                f"项目目录文件已生成/更新:{_env_lines}"
                            ]
                        debug.log(
                            "environment.deliverable.project_file",
                            "deliverables exist in environment project root; "
                            "removed from chat attachment delivery check",
                            sorted(_env_project_files),
                        )
                    if not missing:
                        debug.log(
                            "workspace.missing.environment_resolved",
                            "all missing deliverables were environment project files",
                            sorted(_env_project_files),
                        )
                if missing:
                    debug.log("workspace.missing", f"deliverables not found: {missing}")
                    # 2026-05-10 Patch 56:missing 文件**强约束** round3,不能让模型说"已发"。
                    # 病因(trace b430c4f228eb40c7):helper 报告的 deliverable 没被 promote
                    # 到主区(常见原因:文件名前缀不一致 / helper interrupted/stuck 没真正产出 /
                    # forced finalize 路径从 message history 抓 deliverable 但抓错名字)。
                    # P32b 已经处理"文件存在但结构有致命 warning"的强约束,此处补"文件根本不存在"。
                    # 修法:与 P32b 同等强度 — 从 plan.deliverables 移除 + plan.delivery_partial
                    # 加入 + 改写 plan.intent,round3 看到"原 deliverables"里没这些文件,自然不会
                    # 在文本里说"已发给你"。
                    _missing_set = set(missing)
                    if plan.deliverables:
                        plan.deliverables = [
                            d for d in plan.deliverables if d not in _missing_set
                        ]
                    _existing_partial = set(plan.delivery_partial or [])
                    plan.delivery_partial = sorted(_existing_partial | _missing_set)
                    debug.log(
                        "workspace.missing.demote",
                        f"P56: 把 {len(_missing_set)} 个未找到的 deliverable 从 plan.deliverables "
                        f"移到 delivery_partial,改写 plan.intent 阻止 round3 说已发",
                        sorted(_missing_set),
                    )
                    _miss_lines = "\n".join(f"  - {fn}" for fn in sorted(_missing_set))
                    _orig_intent = (plan.intent or "")[:200]
                    _remaining_ok = len(plan.deliverables or []) + len(generated_files)
                    _missing_fact = (
                        "Delivery fact: these planned deliverable files do not exist in the chat workspace and therefore cannot be attached:\n"
                        f"{_miss_lines}\n\n"
                        f"Existing attached file count after physical checks: {_remaining_ok}. "
                        f"Original intent reference: {_orig_intent}\n"
                        "交付事实：上述文件不存在，物理上无法推送；其它已存在附件按实际列表处理。"
                    )
                    plan.intent = (
                        "Reply in persona using the delivery facts below. Do not state that a missing file was sent; decide the user-facing wording from the facts.\n"
                        + _missing_fact
                    )
                    plan.key_points = list(plan.key_points or []) + [_missing_fact]

            # ── 2026-05-09 Patch 32b: 代码级 deliverable 结构验收 ──
            # 病因(trace 779bbcf0):helper 出 docx 219 段 8455 字 + image_count=0,
            # 主线程 inspect_file 看到 0 张图,但 prompt 级硬规则被无视,仍列入交付。
            # 修法:这里在 promote 前对每个**二进制 deliverable** 调 inspect_file,
            # 命中"致命级 warning"(完全空白/极短/0 幻灯片/0 图等)的 deliverable
            # 从 generated_files 移除,加入 plan.delivery_partial。然后改写 plan.intent
            # 让 Round 3 诚实告诉用户"这些做了但质量不达标,先不推送"而不是说"已交付"。
            # 仅检查 docx/pptx/xlsx/pdf/png/jpg/webp/bmp/gif — 源码 / .txt 不需要结构性检查。
            _BIN_DELIVERABLE_EXTS = {
                ".docx", ".pptx", ".xlsx", ".pdf",
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
            }
            # 致命 warning 只保留物理/结构边界。质量类 warning(如"极短"/"长文档无图")
            # 作为事实交给 LLM 处理,不在推送层替模型判定质量是否可交付。
            _FATAL_WARN_KEYWORDS = (
                "完全空白", "0 幻灯片", "所有 sheet 均空", "尺寸 0×0",
                "文件大小为 0",
            )
            _validation_failures: list[tuple[str, list[str]]] = []  # (fname, fatal_warnings)
            _validation_warnings: list[tuple[str, list[str]]] = []  # 非致命 warning 也 log
            for _fname, _url, _full_path in list(generated_files):
                _ext = os.path.splitext(_fname)[1].lower()
                if _ext not in _BIN_DELIVERABLE_EXTS:
                    continue
                try:
                    _inspection = ws_tool.inspect_file(workspace_dir, _fname)
                except Exception as _e_inspect:
                    debug.log(
                        "workspace.deliverable.inspect_error",
                        f"inspect_file 失败,放行 {_fname}: {type(_e_inspect).__name__}: {_e_inspect}",
                    )
                    continue
                if not _inspection.get("ok"):
                    # 文件无法解析 → 视为致命(可能是 0 字节/损坏/非真正的二进制)
                    _validation_failures.append((
                        _fname,
                        [f"inspect_file 失败: {_inspection.get('error', '?')}"],
                    ))
                    continue
                _ws_warnings = _inspection.get("warnings") or []
                if not _ws_warnings:
                    continue  # 干净
                _fatal = [w for w in _ws_warnings if any(kw in w for kw in _FATAL_WARN_KEYWORDS)]
                _nonfatal = [w for w in _ws_warnings if w not in _fatal]
                if _fatal:
                    _validation_failures.append((_fname, _fatal))
                if _nonfatal:
                    _validation_warnings.append((_fname, _nonfatal))

            if _validation_warnings:
                debug.log(
                    "workspace.deliverable.warning",
                    f"{len(_validation_warnings)} deliverable(s) 有非致命 warning(允许交付)",
                    {f: ws for f, ws in _validation_warnings},
                )
                _warning_lines = []
                for _fn, _ws in _validation_warnings:
                    if _ws:
                        _warning_lines.append(f"{_fn}: " + "; ".join(str(w) for w in _ws[:3]))
                if _warning_lines:
                    plan.key_points = list(plan.key_points or []) + [
                        "Delivery fact: structural inspection reported non-fatal warning(s), but the file(s) passed physical delivery checks: "
                        + " | ".join(_warning_lines)
                    ]

            if _validation_failures:
                # 把致命 fail 的 deliverable 从 generated_files 移除
                _failed_names = {fn for fn, _ in _validation_failures}
                generated_files = [
                    (f, u, p) for f, u, p in generated_files if f not in _failed_names
                ]
                # 加入 plan.delivery_partial(去重)
                _existing_partial = set(plan.delivery_partial or [])
                plan.delivery_partial = sorted(_existing_partial | _failed_names)
                debug.log(
                    "workspace.deliverable.validation_failed",
                    f"{len(_validation_failures)} deliverable(s) 因致命结构 warning 被拒,"
                    f"移入 delivery_partial: {sorted(_failed_names)}",
                    {f: ws for f, ws in _validation_failures},
                )
                # 改写 plan.intent 让 Round 3 诚实交代
                # 2026-05-09 二次审计修:区分"全部被拒"和"部分被拒"两种场景。
                # 之前措辞统一是"不推送",但部分被拒时其他 deliverable 仍会正常推 → 与
                # Patch 35"已发"提示矛盾,Round 3 会误以为全没发。
                _fail_lines = []
                for _fn, _ws in _validation_failures:
                    _reason = _ws[0] if _ws else "未知问题"
                    # 去掉 ⚠️ 前缀,人设回复时不要带这个 emoji
                    _reason = _reason.replace("⚠️", "").strip()
                    _fail_lines.append(f"  - {_fn}: {_reason}")
                _orig_intent = (plan.intent or "")[:200]
                # 当前剩余有效 deliverable 数 = generated_files 已被移除失败项后的长度
                _remaining_ok_count = len(generated_files)
                if _remaining_ok_count == 0:
                    # 全部被拒:一个都没推
                    plan.intent = (
                        "Reply in persona using the delivery facts below. The listed files failed physical/structural delivery checks and were not attached:\n"
                        + "\n".join(_fail_lines) + "\n\n"
                        f"Original intent reference: {_orig_intent}\n"
                        "交付事实：上述文件未通过物理/结构检查，未推送。"
                    )
                else:
                    # 部分被拒:其他通过的会正常推送(由 Patch 35 引导措辞),这里只描述"哪些没推"
                    plan.intent = (
                        "Reply in persona using the delivery facts below. Some files passed attachment checks; the following files failed physical/structural delivery checks and were not attached:\n"
                        + "\n".join(_fail_lines) + "\n\n"
                        f"Original intent reference: {_orig_intent}\n"
                        "交付事实：通过检查的附件按实际列表处理；上述文件未推送。"
                    )
                # 同时把检查出问题的内容塞进 internal_note,便于 maintenance 写记忆
                plan.internal_note = (
                    (plan.internal_note or "") + " | "
                    f"deliverable_validation_failed: {sorted(_failed_names)} (Patch 32b 拒交付)"
                )

            # ── 2026-05-03 Bug E:把交付物从 .temp/ 提升到主工作区 ──
            # 主工作区是干净的核心成果区,只放 plan.deliverables 里列的文件
            # (+ 模型主动 commit_to_main 推送的)。中间产物(_helper_*前缀文件、
            # 调试 .json、benchmark 中间结果等)都留在 .temp/ 不污染主区。
            # 下一次 chat 启动时,主区会被 sync 到 .temp,所以"下次还能看到"
            # 不依赖中间文件留在主区。
            if main_workspace_dir and want:
                _to_promote = sorted(
                    {f for f, _, _ in generated_files}
                    | {d for d in want if d in {f for f, _, _ in generated_files}}
                )
                if _to_promote:
                    try:
                        promoted, skipped, name_remap = ws_tool.promote_to_main(
                            main_workspace_dir, workspace_dir, list(_to_promote),
                        )
                        promoted_to_main_total = list(promoted)
                        # 2026-06-12: 提升后把 generated_files 的 local_path 更新到主区
                        # 否则 done.files 的 local_path 指向 .temp/,bridge 做
                        # upload_group_file 时可能因 .temp/ 被清理而找不到文件。
                        # 也覆盖 skipped (已在主区) 的情况:重推文件不会被 promote,但仍需
                        # 把 local_path 从 .temp/ 改写为主区路径。
                        _main_fp_map: dict[str, str] = {}
                        for _pb in promoted_to_main_total + list(skipped):
                            _mp = os.path.join(main_workspace_dir, _pb)
                            if os.path.isfile(_mp):
                                _main_fp_map[_pb] = _mp
                        if _main_fp_map:
                            generated_files = [
                                (_f, _u, _main_fp_map.get(os.path.basename(_f), _p))
                                for _f, _u, _p in generated_files
                            ]
                        if skipped:
                            _existing_partial = set(plan.delivery_partial or [])
                            plan.delivery_partial = sorted(_existing_partial | set(skipped))
                        if promoted:
                            debug.log(
                                "workspace.promote.auto",
                                f"自动提升 {len(promoted)} 个 deliverables 到主区: "
                                f"{promoted[:6]}"
                                + (f" ... +{len(promoted)-6}" if len(promoted) > 6 else ""),
                            )
                        if name_remap:
                            debug.log(
                                "workspace.promote.remap",
                                f"文件名映射: {name_remap}",
                            )
                        if skipped:
                            debug.log(
                                "workspace.promote.skipped",
                                f"未能提升: {skipped[:6]}",
                            )
                    except Exception as e:
                        log.warning(
                            "auto promote failed (%s), skipping: %s",
                            type(e).__name__, e,
                        )
                        debug.error(
                            f"auto promote failed: {e}\n"
                            f"files to promote: {_to_promote[:6]}"
                            + (f" ... +{len(_to_promote)-6}" if len(_to_promote) > 6 else ""),
                        )
                        promoted_to_main_total = []
                        _existing_partial = set(plan.delivery_partial or [])
                        plan.delivery_partial = sorted(_existing_partial | set(_to_promote))

        # ── 2026-05-09 Patch 34: > 3 个 deliverable 自动打 zip ──
        # 病因:napcat 端把每个 deliverable 单独发群文件,> 3 个会刷屏。
        # 设计:
        #   - 仅当成功 promote 的 deliverable 数 > 3 时触发(.txt/.docx/源码任意都算)
        #   - 打包 zip 放主区,**替换** generated_files 为单个 zip 条目,
        #     done_payload["files"] 也只看到 zip
        #   - 失败(磁盘 / zipfile 异常)时降级:保留原 generated_files,继续按多文件交付
        #   - plan.deliverables 不动(供下次 chat 回顾"实际产出哪些"),仅 SSE done.files 收敛
        # 2026-05-09 二次审计修:用 promoted_to_main_total(basename 列表)而不是
        #   generated_files 的 fname(可能含子路径如 "subdir/x.txt")作为 zip 输入。
        #   create_zip_archive 内部会跳过非 basename 项,以前会丢失子目录文件。
        _ZIP_THRESHOLD = 3
        _zipped = False  # 2026-05-09 加:标记本轮是否打了 zip,Round 3 用以调整措辞
        _zip_member_count = 0
        if (
            main_workspace_dir
            and len(promoted_to_main_total) > _ZIP_THRESHOLD
            and promoted_to_main_total  # 必须有成功 promote 才能打包
        ):
            try:
                _zip_inputs = list(promoted_to_main_total)  # 已是 basename
                _zip_result = ws_tool.create_zip_archive(
                    main_workspace_dir, _zip_inputs,
                    trace_id=trace_id,
                    archive_id=req.archive_id,
                    group_id=req.group_id,
                )
                if _zip_result is not None:
                    _zip_name, _zip_url, _zip_full = _zip_result
                    debug.log(
                        "workspace.zip.replace",
                        f"deliverables {len(promoted_to_main_total)} > {_ZIP_THRESHOLD},"
                        f"合并为 {_zip_name},done.files 只发 zip",
                    )
                    # 用 zip 替换 generated_files(供 done_payload 取)
                    # promoted_to_main_total / plan.deliverables 保持不变(用于 bot_log / 记忆)
                    generated_files = [(_zip_name, _zip_url, _zip_full)]
                    _zipped = True
                    _zip_member_count = len(_zip_inputs)
                else:
                    debug.log(
                        "workspace.zip.failed",
                        "create_zip_archive 返回 None,降级为多文件交付",
                    )
            except Exception as _e_zip:
                log.warning("auto-zip failed: %s", _e_zip, exc_info=True)
                debug.error(
                    f"auto-zip failed: {type(_e_zip).__name__}: {_e_zip},降级为多文件交付"
                )

        # Round2 可显式指定:把 TTS 结果作为最终语音回复,而不是普通文件附件。
        _round2_voice_reply_file = (plan.voice_reply_file or "").strip()
        _round2_voice_reply_text = (plan.voice_reply_text or "").strip()
        if _round2_voice_reply_file:
            _vf_base = os.path.basename(_round2_voice_reply_file)
            _all_ws_files = set(ws_tool.list_generated_files(workspace_dir)) if workspace_dir else set()
            if _vf_base in _all_ws_files:
                _allow_vf, _vf_reason = await _persona_voice_reply_guard(
                    persona or "", req.message or "", _vf_base,
                )
                debug.log(
                    "voice.round2_handoff.guard",
                    f"file={_vf_base!r} allow={_allow_vf} reason={_vf_reason}",
                )
                if _allow_vf:
                    plan.deliverables = [d for d in (plan.deliverables or []) if os.path.basename(d) != _vf_base]
                    generated_files = [g for g in generated_files if os.path.basename(g[0]) != _vf_base]
                    plan.key_points = [f"按 Round2 指定,最终回复使用已生成语音:{_vf_base}"]
                    plan.length_hint = "短"
                else:
                    plan.voice_reply_file = ""
                    _round2_voice_reply_file = ""
                    plan.intent = "按人设拒绝把这段 TTS 作为最终语音回复"
                    plan.key_points = [_vf_reason or "人设守卫拒绝语音最终输出"]
                    plan.deliverables = [d for d in (plan.deliverables or []) if os.path.basename(d) != _vf_base]
            else:
                debug.log("voice.round2_handoff.missing", f"voice_reply_file not found: {_vf_base!r}")
                plan.voice_reply_file = ""
                _round2_voice_reply_file = ""
        elif _round2_voice_reply_text:
            _allow_vt, _vt_reason = await _persona_voice_reply_guard(
                persona or "", req.message or "", _round2_voice_reply_text,
            )
            debug.log(
                "voice.round2_handoff.guard",
                f"text_len={len(_round2_voice_reply_text)} allow={_allow_vt} reason={_vt_reason}",
            )
            if _allow_vt:
                plan.key_points = [_round2_voice_reply_text]
                plan.length_hint = "短"
            else:
                plan.voice_reply_text = ""
                _round2_voice_reply_text = ""
                plan.intent = "按人设拒绝把这段 TTS 文本作为最终语音回复"
                plan.key_points = [_vt_reason or "人设守卫拒绝语音最终输出"]

        # ── 3. Round 3：人设流式润色 ──
        # Bug 27 修（彻底版）：用 abort generation 号区分轮次。
        #
        # 旧版 Bug 27：Round 2 catch 完 abort 后，Round 3 启动前直接
        # `abort_event.clear()`。如果在这两步之间用户又发了第二条 abort
        # 信号，会被一并清掉，第二个 abort 丢失（典型窗口几毫秒）。
        #
        # 新版：进 Round 3 前 snapshot 当前 gen;Round 3 流式时不再看
        # is_set(),而是看 `abort_ch.gen > round3_start_gen`,这样:
        #   - Round 2 阶段产生的旧 set 状态对 Round 3 不再有效（gen 没递增）
        #   - Round 3 流式期间到达的新 abort 信号会让 gen 递增,被立即识别
        # （顺便保留了"R2 是否曾被 abort"的语义,见 r2_was_aborted）
        #
        # 2026-05-25: 进入 Round 3 后标记 stage="round3",bridge/abort 端点
        # 检测到此阶段会拒绝 abort,只排队等流式回复自然结束。避免 abort 抢在
        # token 之前导致 final_text 留空、用户收不到任何回复。
        guard.set_stage(req.archive_id, req.group_id, req.user_id, "round3")
        r2_was_aborted = abort_ch.gen > _round_start_gen
        round3_start_gen = abort_ch.gen
        # 同步把 Event 清掉,因为底层 _round3 代码暂时还在用 is_set() 检查;
        # gen-based 检查也会同时启用（见下方 streaming 循环里的兜底）。
        if abort_event.is_set():
            abort_event.clear()
        yield "progress", _progress_payload("composing_reply", "responding", "正在组织回复")
        debug.section("ROUND 3 — persona response (streaming)")
        final_text_parts: list[str] = []
        _round3_buffer_until_safe = True
        _round3_buffer_limit = 1200
        _round3_buffer_flushed = False
        _gen_files_2tuple = [(f, u) for f, u, _ in generated_files] if generated_files else None

        # ── 2026-05-10 Patch 58 v2: round3 输入文件名去内部前缀 ──
        # 用户原话:"推送时有时候会带有内部维护的文件名,实际不需要,会暴露智能体内部结构"
        # 之前 P58 只改了 napcat 推送的 `name` 字段,但 round3 模型看到的 plan.deliverables /
        # plan.delivery_partial / files 列表仍是带前缀的内部名 — 模型可能在文本里写出来。
        # 修法:round3 调用前,把这三个字段通过 displayed_name_remap 映射为去前缀的用户友好名。
        # 主线程的 plan 本体保持不变(后续 maintenance / bot_log / 内存 用)。
        _displayed_remap_for_r3 = _load_displayed_name_remap_for_delivery(
            main_workspace_dir,
            workspace_dir,
        )
        # Fallback:partial 文件名等可能没在 mapping 中(P56 demote 触发时文件根本没生成,
        # 没经过 promote 没写 mapping)。这种情况用 sibling task_id 集合剥前缀作为兜底。
        try:
            from app.core.core_processes import proc_registry, ProcessRegistry
            _main_owner_for_strip = ProcessRegistry.make_main_owner(trace_id)
            _sibling_helpers = await proc_registry().list_owned_by(_main_owner_for_strip)
            _known_task_id_prefixes = sorted(
                {(h.get("helper_task_id") or "") for h in _sibling_helpers if h.get("helper_task_id")},
                key=len, reverse=True,  # 长前缀优先,避免短前缀误匹配长 task_id
            )
        except Exception:
            _known_task_id_prefixes = []

        def _displayed_name(name: str) -> str:
            """先查 mapping,再用 task_id 前缀剥(fallback)。"""
            if name in _displayed_remap_for_r3:
                return _displayed_remap_for_r3[name]
            for tid in _known_task_id_prefixes:
                pfx = tid + "_"
                if name.startswith(pfx):
                    stripped = name[len(pfx):]
                    if stripped:
                        return stripped
            return name

        if _gen_files_2tuple:
            _gen_files_2tuple = [(_displayed_name(f), u) for f, u in _gen_files_2tuple]
        # 给 round3 用 plan 副本(避免修改主流程的 plan)
        if (_displayed_remap_for_r3 or _known_task_id_prefixes) and (
            plan.deliverables or plan.delivery_partial
        ):
            try:
                _plan_for_r3 = type(plan)(**plan.model_dump())
                if _plan_for_r3.deliverables:
                    _plan_for_r3.deliverables = [
                        _displayed_name(d) for d in _plan_for_r3.deliverables
                    ]
                if _plan_for_r3.delivery_partial:
                    _plan_for_r3.delivery_partial = [
                        _displayed_name(d) for d in _plan_for_r3.delivery_partial
                    ]
                debug.log(
                    "round3.displayed_name_applied",
                    f"P58 v2: round3 输入文件名已去前缀 "
                    f"(mapping={len(_displayed_remap_for_r3)} 条, "
                    f"task_id 前缀={len(_known_task_id_prefixes)} 个)",
                )
            except Exception:
                log.exception("P58 v2 plan copy failed (non-fatal); using original plan")
                _plan_for_r3 = plan
        else:
            _plan_for_r3 = plan

        # ── 2026-05-02 part9 加:Round 2 期间已 abort → Round 3 不发 LLM 请求 ──
        # 旧行为:即使 r2_was_aborted=True,Round 3 仍调用 _round3() 发 LLM stream
        # 生成 200-300 字回复,但用户已经按 abort 走了 — 这段 LLM 输出主要给 hot
        # memory + pause snapshot 留底,不是给当前用户看。但等 LLM 仍占 chat 流程
        # 3-15s,延迟用户感知到的"chat 完结"和 per-user 锁的释放。
        # 新行为:r2_was_aborted=True 时直接渲染一段简短的"被打断"占位符,跳过 LLM。
        # 占位符已经够下一轮 bot 看 hot memory 时知道"上次被打断了",不需要 LLM
        # 重新生成。pause snapshot 会单独保存 plan + active helper 列表,信息完整度
        # 不受影响。
        #
        # 2026-05-02 part19 修复:之前占位符是
        #   f"(回复被打断,上一轮任务未完整呈现) 当时计划:{_intent}"
        # 这有两个严重问题:
        # 1. 暴露系统语言("回复被打断"/"任务未完整呈现"是元描述,破坏人设)
        # 2. 拼接 plan.intent — 这是模型内部笔记,可能含 "L445: codes[i].code 从未
        #    被设置..." 这种调试细节,直接 yield 给用户看是泄露内部状态
        # 实测 trace 74769ad9 用户原话抱怨:"这种内容不要输出,影响人设"。
        # 修法:用一段**完全中性**的极短占位文本,不含 plan 字段、不含元术语。
        # bot_log 标签里用结构化 marker 让下次 bot 看 hot 时知道"上次被打断";
        # 当次用户看到的是干净的中性短句,与人设兼容。
        _voice_pref = 0.0
        try:
            from app.memory.persona_files import persona_voice_preference_by_content
            _voice_pref = persona_voice_preference_by_content(persona or "", 0.0)
        except Exception:
            _voice_pref = 0.0
        try:
            from app.core.runtime_mode import is_environment_mode
            if is_environment_mode():
                _voice_pref = 0.0
        except Exception:
            pass
        debug.log("voice.preference", f"value={_voice_pref:.2f}")

        _literal_final_reply = _extract_requested_literal_reply(req.message)
        _can_skip_round3_literal = (
            bool(_literal_final_reply)
            and not r2_was_aborted
            and not generated_files
            and not (plan.deliverables or plan.delivery_partial)
            and not (plan.voice_reply_text or plan.voice_reply_file)
        )
        if r2_was_aborted:
            injected_interrupt_messages: list[str] = []
            if interrupt_messages_getter is not None:
                try:
                    injected_interrupt_messages = [
                        str(m).strip()
                        for m in (interrupt_messages_getter() or [])
                        if str(m).strip()
                    ]
                except Exception as _e_intr:
                    debug.log(
                        "round2_5.interrupt_messages.error",
                        f"failed to collect interrupt messages: {type(_e_intr).__name__}: {_e_intr}",
                    )
                    injected_interrupt_messages = []
            if injected_interrupt_messages:
                debug.log(
                    "round2_5.interrupt_messages",
                    f"injecting {len(injected_interrupt_messages)} queued user message(s) into abort summary",
                    {"messages": injected_interrupt_messages[:3]},
                )
                # 2026-05-21: 登记这些已被合并回答的打断消息指纹,
                # 让随后作为新 /chat 请求进来的同一条消息在 stop_mode 入口被识别并跳过,
                # 避免\"abort 总结 + stop_mode 强制 easy\"双回复(实测 trace 13cedd4e 10:38)。
                try:
                    get_group_guard().mark_abort_injected(
                        req.archive_id, req.group_id, req.user_id,
                        injected_interrupt_messages,
                    )
                except Exception as _e_mark:
                    debug.log(
                        "round2_5.mark_injected.error",
                        f"failed to mark abort-injected messages: {type(_e_mark).__name__}: {_e_mark}",
                    )
            # ── 2026-05-04 Bug #16 修复(用户最痛的体验) ──
            # 旧版完全静默(final_text=""),用户按"停"后屏幕一片空白 → 32 分钟白等
            # 不知道做到哪。trace f3a3aafb 实测:用户体验是"按了停 → 屏幕没字 →
            # 不知道发生了啥",force-kill 才能收场。
            #
            # 修法(用户原话):"用户只要在中途打断,直接进入 round3 总结就行,一定能停"
            # 用户嫌的不是"看到字",是"看到一句敷衍占位符"。把它和"看到一份诚实的状态
            # 总结"混为一谈,然后干脆全噤声,就成了 32 分钟白等。
            #
            # 新行为:仍走 _round3() 流式,但用一个 abort-aware 的特化 plan:
            #   - intent: 让 round3 给"做完了什么/还差什么/产出在哪"的简短交代
            #   - key_points: 拼装当前已 commit 文件 + 原 plan.intent + "工作区保留"
            #   - length_hint="短": 一两句话即可
            #   - avoid: 不要承诺重新开始,不要假装继续
            # 这样:
            #   ✓ 用户绝对不会再 32 分钟白等
            #   ✓ Round 3 LLM 流式天然受 abort_event 控制(再次 abort 即停)
            #   ✓ bot_log 仍按原逻辑写入,下次 bot 看历史时知道发生了什么
            debug.log(
                "round3.abort_summary",
                "Round 2 期间 abort 检测到 → 走 round3 总结路径(给用户一句交代,而非静默)",
            )

            # 拼装 abort-aware plan
            _committed_str = ""
            if generated_files:
                _names = [
                    (f.split("/")[-1].split("\\")[-1])
                    for f, _u, _ in generated_files[:6]
                ]
                _committed_str = ", ".join(_names)
                if len(generated_files) > 6:
                    _committed_str += f" ... +{len(generated_files)-6}"

            _orig_intent = (plan.intent if plan else "").strip()
            # ── 2026-05-04 Bug #16 v2 改进:净化 plan.intent ──
            # 原 intent 可能含调试细节 / 工具调用片段 / 冒号路径(实测 trace 74769ad9
            # 用户原话:"L445: codes[i].code 从未被设置 → 这种内容不要输出,影响人设")。
            # 在拼进 key_points 之前过滤明显的代码/系统语言痕迹。
            _orig_intent = _orig_intent[:200]
            _looks_internal = any(
                marker in _orig_intent for marker in (
                    "L#", "L_", "::", "->", "→",  # 函数调用/箭头
                    ".c:", ".py:", ".h:", ".cpp:",  # 源码定位
                    "0x", "undefined reference",
                    "tool=", "helper=", "iter=",
                )
            )
            if _looks_internal:
                # 含内部细节就降级为通用描述
                _orig_intent = ""
            _abort_key_points = []
            if _committed_str:
                _abort_key_points.append(f"已完成: {_committed_str}")
            else:
                _abort_key_points.append("尚未产出可固化的文件")
            if _orig_intent:
                _abort_key_points.append(f"未完成: {_orig_intent}")
            else:
                _abort_key_points.append("未完成: 规划阶段就被打断")
            if workspace_dir:
                _abort_key_points.append("工作区已保留,后续可继续")
            if injected_interrupt_messages:
                _joined_interrupts = " / ".join(injected_interrupt_messages[:3])[:500]
                _abort_key_points.append(f"用户打断时补充询问:{_joined_interrupts}")

            _abort_tone = (plan.tone if plan and plan.tone else "自然")
            abort_plan = ResponsePlan(
                intent=(
                    "The user interrupted mid-task. Reply briefly in persona using only what actually happened this turn: "
                    "what was completed, what remains, and where any output is. If key_points contain an extra question inserted during interruption, answer it in the same reply.\n"
                    "用户中途打断时，按本轮事实简短说明完成项、缺口和产物；插入问题同条回答。"
                ),
                key_points=_abort_key_points,
                tone=_abort_tone,
                length_hint="短",
                avoid=[
                    "Treat the interruption as a stop request unless the user explicitly asked to resume.\n默认按停止处理，除非用户明确要求继续。",
                    "Describe no ongoing work after the stop; only mention preserved state or completed outputs.\n停止后不描述仍在工作，只说明保留状态或已完成产物。",
                    "Keep apology short if needed.\n道歉要短。",
                    "Use user-facing terms instead of internal workflow names.\n用用户可理解的说法替代内部流程名。",
                    "Address any extra interruption-time question included in key_points.\n回答打断时追加的问题。",
                ],
                callbacks=[],
                internal_note="用户中途按停,本次走 abort-aware 总结路径; Round2.5 已合并打断时新询问",
                deliverables=[],
            )

            # 走 _round3() 但用 abort_plan + 强制 lite 模型(快、且对短回复够用)
            async for tok in _round3(
                persona, abort_plan, req.user_name, req.message, hot_user,
                light=True, files=_gen_files_2tuple,
                abort_event=abort_event,
                in_flight_others=in_flight_others or None,
                recent_group_messages=recent_group_msgs or None,
                helper_reports_excerpt=None,  # abort 路径不再 dump helper 报告(已在 key_points)
                think=False, tier="low",
            ):
                final_text_parts.append(tok)
                if _round3_buffer_until_safe:
                    _prefix = "".join(final_text_parts)
                    if _looks_like_user_visible_protocol_text(_prefix):
                        debug.warn(
                            "round3.protocol_leak.blocked",
                            "blocked abort-summary Round3 output containing hidden tool protocol",
                            _prefix[:1000],
                        )
                        final_text_parts = [_plan_fallback_user_reply(abort_plan)]
                        _round3_buffer_flushed = True
                        yield "token", {"text": final_text_parts[0]}
                        break
                    if len(_prefix) >= _round3_buffer_limit:
                        _round3_buffer_until_safe = False
                        _round3_buffer_flushed = True
                        yield "token", {"text": _prefix}
                else:
                    if _looks_like_user_visible_protocol_text(tok):
                        debug.warn(
                            "round3.protocol_leak.blocked_late",
                            "blocked late abort-summary Round3 token containing hidden tool protocol",
                            tok[:500],
                        )
                        break
                    yield "token", {"text": tok}
                # 流式期间又来新 abort → 立即停(用户连按两次)
                if abort_ch.gen > round3_start_gen:
                    debug.log(
                        "round3.abort_summary.second_abort",
                        "abort 总结流式期间又来新 abort,立即停止 yield",
                    )
                    break

            final_text = "".join(final_text_parts)
            if (
                _round3_buffer_until_safe
                and final_text.strip()
                and not _round3_buffer_flushed
            ):
                yield "token", {"text": final_text}
        elif _can_skip_round3_literal:
            debug.log(
                "round3.literal_reply_shortcut",
                "tools completed; using user-requested literal final reply without Round3 LLM",
                _literal_final_reply,
            )
            final_text = _literal_final_reply
            final_text_parts = [final_text]
            yield "token", {"text": final_text}
        else:
            # ── Round 3 模型选择 ──
            # 质量优先: Round 3 不只是润色,还负责按人设组织事实、避免内部实现泄露、
            # 处理上下文追问和交付措辞。只有真正 trivial / 字面短回复这类低风险回合
            # 才继续用 lite; 普通技术问答、上下文追问和工具任务收尾用 mid 档。
            _length_hint = (plan.length_hint or "").strip() if plan else ""
            _is_long_reply = _length_hint in ("长", "详细", "long", "Long", "LONG")
            _low_risk_lite_reply = bool(is_trivial or _is_direct_short_reply_request(req.message))
            _round3_use_lite = (
                getattr(settings, "lite_round3_for_easy", True)
                and not _is_long_reply
                and complexity == "easy"
                and _low_risk_lite_reply
            )
            _r3_think = not _round3_use_lite
            _r3_tier = "low" if _round3_use_lite else "mid"
            if _round3_use_lite:
                debug.log(
                    "round3.model_choice",
                    f"using lite model (complexity={complexity}, length_hint={_length_hint!r})",
                )
            else:
                # 2026-05-07 Opt G: 反向监控 — 记录"本该用 lite 但没用的"场景
                _reasons = []
                if not getattr(settings, "lite_round3_for_easy", True):
                    _reasons.append("config:lite_round3_for_easy=False")
                if _is_long_reply:
                    _reasons.append(f"length_hint={_length_hint}")
                elif complexity != "easy":
                    _reasons.append(f"complexity={complexity}")
                elif not _low_risk_lite_reply:
                    _reasons.append("not low-risk trivial/literal reply")
                if not _reasons:
                    _reasons.append("unknown")
                debug.log(
                    "round3.model_choice",
                    f"using main model (lite skipped: {'; '.join(_reasons)})",
                )

            # 2026-05-02 part10 P5:把累积的 helper excerpts 转成 round3 期望的格式
            # [{"task_id": ..., "excerpt": ...}],只在非 easy 路径(easy 路径不调 helper)
            _excerpts_for_r3 = None
            if all_helper_excerpts or all_verification_needed:
                _excerpts_for_r3 = []
                # 2026-05-06: 有未验证 helper 产出时,在前面插入警告条目
                if all_verification_needed:
                    _excerpts_for_r3.append({
                        "task_id": "⚠️ 验证警告",
                        "excerpt": (
                            f"以下 helper 报告**未经独立验证**,可能包含未发现的 bug。"
                            f"用户追问准确性时,应说明结果尚未独立验证,不要当作确凿事实引用。"
                            f"验证相关事实: {all_verification_advice[:300]}"
                        ),
                    })
                for tid, text in all_helper_excerpts.items():
                    if text and text.strip():
                        _excerpts_for_r3.append({"task_id": tid, "excerpt": text})

            # 2026-05-16 Round 14b: 三者并行 round3.
            # 用户设计 (引用): "三者并行,决策出来后可以任选一个,而不是输出了才选择一边跑".
            # 内部启动: 1) lite 决策 task  2) 文字版 round3  3) 语音版 round3
            # 决策出来 (几百 ms) 后 cancel 败者, flush 胜者 buffer + 续 stream.
            # 资源代价 2x round3 LLM 调用; 换用户 0 延迟感知.
            
            async for tok in _round3_parallel(persona, _plan_for_r3, req.user_name, req.message, hot_user,
                                      light=(complexity != "easy"), files=_gen_files_2tuple,
                                      abort_event=abort_event,
                                      in_flight_others=in_flight_others or None,
                                      recent_group_messages=recent_group_msgs or None,
                                      helper_reports_excerpt=_excerpts_for_r3,
                                      think=_r3_think, tier=_r3_tier,
                delivered_as_zip=_zipped,
                zip_member_count=_zip_member_count,
                voice_preference=_voice_pref):
                final_text_parts.append(tok)
                if _round3_buffer_until_safe:
                    _prefix = "".join(final_text_parts)
                    if _looks_like_user_visible_protocol_text(_prefix):
                        debug.warn(
                            "round3.protocol_leak.blocked",
                            "blocked Round3 output containing hidden tool protocol",
                            _prefix[:1000],
                        )
                        final_text_parts = [_plan_fallback_user_reply(_plan_for_r3)]
                        _round3_buffer_flushed = True
                        yield "token", {"text": final_text_parts[0]}
                        break
                    if len(_prefix) >= _round3_buffer_limit:
                        _round3_buffer_until_safe = False
                        _round3_buffer_flushed = True
                        yield "token", {"text": _prefix}
                else:
                    if _looks_like_user_visible_protocol_text(tok):
                        debug.warn(
                            "round3.protocol_leak.blocked_late",
                            "blocked late Round3 token containing hidden tool protocol",
                            tok[:500],
                        )
                        break
                    yield "token", {"text": tok}
                # gen-based 兜底：流式期间 abort_ch.gen 上涨说明有新 abort 到达。
                # 此时 abort_event 也会被 signal() 设置,_round3 内的 is_set() 检查
                # 也会终止循环;这里再补一道,确保即使 _round3 还没读到也能停。
                if abort_ch.gen > round3_start_gen:
                    break

            final_text = "".join(final_text_parts)
            if (
                _round3_buffer_until_safe
                and final_text.strip()
                and not _round3_buffer_flushed
            ):
                yield "token", {"text": final_text}
        if not final_text.strip() and not (abort_ch.gen > round3_start_gen):
            final_text = (
                "这轮没有可靠完成，最终回复没有成功生成。请稍后重试或发送“继续”，"
                "我会从当前状态重新处理。"
            )
            final_text_parts = [final_text]
            debug.warn(
                "round3.empty_fallback: Round3 produced no user-visible text; "
                "emitted non-LLM fallback reply"
            )
            yield "token", {"text": final_text}
        debug.log("round3.done", f"len={len(final_text)}", final_text)
        _mark("round3_done")

        # ── 语音输出决策 ──
        voice_reply_file: tuple[str, str, str] | None = None  # (rel_path, url, local_path)
        voice_suppress_text = False
        try:
            from app.llm.voice_output import decide_voice, VoiceDecision
            from app.llm.tools.tts_bridge import tts_design, is_available as tts_available

            decision: VoiceDecision | None = None
            if _round2_voice_reply_file:
                _vf = os.path.basename(_round2_voice_reply_file)
                _vp = os.path.join(workspace_dir, _vf)
                if os.path.isfile(_vp):
                    voice_reply_file = (
                        _vf,
                        f"/v1/chat/files/{req.archive_id}/{req.group_id}/{_vf}",
                        _vp,
                    )
                    voice_suppress_text = True
                    debug.log("voice.round2_handoff", f"using Round2 TTS output as final voice reply: {_vf}")
            elif _round2_voice_reply_text:
                from app.llm.voice_output import _clean_voice_text as _clean_vt
                _cleaned_r2_voice = _clean_vt(_round2_voice_reply_text)
                decision = VoiceDecision(
                    use_voice=bool(_cleaned_r2_voice),
                    voice_text=_cleaned_r2_voice,
                    reason="round2 explicit voice_reply_text handoff",
                )
                debug.log("voice.round2_handoff", f"using Round2 voice_reply_text len={len(_cleaned_r2_voice)}")

            if final_text.strip() and tts_available() and voice_reply_file is None:
                from app.llm.tools.registry import current_voice_ref_audio as _current_voice_ref_audio
                _voice_ref_audio = _current_voice_ref_audio()
                if decision is None:
                    decision = await decide_voice(
                        reply_text=final_text,
                        user_message=req.message or "",
                        persona=persona or "",
                        voice_preference=_voice_pref,
                    )

                if decision.too_long and _is_voice_demanded(req.message or ""):
                    # 用户要求语音但回复太长 → 注入"太长"信息,让下次对话知晓
                    debug.log("voice.too_long",
                              f"reply too long for voice ({_estimate_text_duration(final_text):.0f}s), "
                              "user demanded voice — will note in response")
                    # 在 final_text 末尾追加提示(只在 stored_assistant 中,不影响 UI)
                    pass  # bot_log 会记录

                if decision.use_voice and decision.voice_text:
                    from app.memory.persona_files import persona_voice_instruct_by_content
                    voice_instruct = persona_voice_instruct_by_content(
                        persona or "",
                        default=_extract_voice_instruct(persona or ""),
                    )
                    # 二次清洗: 确保括号内描写不会进入 TTS
                    from app.llm.voice_output import _clean_voice_text as clean_vt
                    speak_text = clean_vt(decision.voice_text)
                    if speak_text and speak_text != decision.voice_text:
                        debug.log(
                            "voice.cleaned_text",
                            f"before={decision.voice_text!r} after={speak_text!r}",
                        )
                    if not speak_text:
                        debug.log("voice.empty_after_clean", "voice_text became empty after cleaning")
                        decision.use_voice = False
                    else:
                        # 生成 TTS 文件到 workspace
                        # 2026-05-09 BUG FIX (Patch 7): 旧版固定 "_voice_reply.wav",上一条语音
                        # 还在下载/播放时,下一条就把文件覆盖 → 用户听到的是新内容,
                        # URL 却没变(QQ 客户端可能 cache),很诡异。
                        # 用毫秒时间戳 + trace 前缀让每条语音独立。
                        import time as _vt
                        _trace_prefix = (debug.current_trace_id() or "")[:8] or "v"
                        voice_fname = f"_voice_{_trace_prefix}_{int(_vt.time() * 1000)}.wav"
                        if not workspace_dir:
                            main_workspace_dir = ws_tool.create_workspace(
                                archive_id=req.archive_id,
                                group_id=req.group_id,
                            )
                            ws_tool.archive_stale_artifacts(
                                main_workspace_dir,
                                max_age_days=14,
                            )
                            _session_tag = (
                                f"{req.archive_id}:{req.group_id}:{req.user_id}:"
                                f"{hash(req.message) & 0xFFFFFFFF:08x}:voice"
                            )
                            workspace_dir = ws_tool.ensure_temp_workspace(
                                main_workspace_dir,
                                session_tag=_session_tag,
                            )
                            ws_tool.register_workspace(group_key, workspace_dir)
                            debug.log(
                                "voice.workspace.lazy_created",
                                f"main={main_workspace_dir} temp={workspace_dir}",
                            )
                        try:
                            os.makedirs(workspace_dir, exist_ok=True)
                        except OSError as _mk_e:
                            raise RuntimeError(f"voice workspace not writable: {workspace_dir}: {_mk_e}") from _mk_e
                        voice_path = os.path.join(workspace_dir, voice_fname)
                        # 2026-05-09 Patch 11 + 2026-05-10 Patch 81:原子写
                        # 让 TTS 写到 .part.wav,生成成功后 os.replace 同盘原子换名。
                        # 中途崩溃只留 .part.wav 半截文件,不会被前端误识为成品(前端只匹配
                        # 不含 .part 的 _voice_*.wav)。
                        #
                        # P81 修(用户报错 21:19 ValueError: Unsupported format: tmp):
                        # 旧路径 voice_path + ".tmp" 让 torchaudio/soundfile_backend 根据
                        # 扩展名推断 format → 拿到 "tmp" → 抛 Unsupported format。
                        # 改成 .part.wav 双段:torchaudio 看到末尾 .wav 走 wav 编码;
                        # 我们用 .part 标记"未完成",前端识别成品照样 OK。
                        voice_path_tmp = voice_path + ".part.wav"
                        # 2026-05-09 Patch 10: 把已检测到的用户语言传给 TTS,避免中英混合
                        # 文本被 OmniVoice 自动识别成错的(常见现象:中文文本被识别成英文然后
                        # 用英文音库朗读 → 拼音化中文,体验灾难)。
                        # _user_lang 在 _round1 之前就检测了,值是 "zh" / "en" / 等 ISO 代码。
                        # OmniVoice 的 language 字段接受 "Chinese" / "English" 等英文名,
                        # 这里做下映射;未知语言直接传 None 走自动识别(向后兼容)。
                        _LANG_NAME_MAP = {
                            "zh": "Chinese", "en": "English",
                            "ja": "Japanese", "ko": "Korean",
                            "fr": "French", "de": "German", "es": "Spanish",
                            "ru": "Russian",
                        }
                        _tts_lang = _LANG_NAME_MAP.get((_user_lang or "").lower())
                        # 2026-05-09 BUG FIX: tts_design 内部 subprocess.run 阻塞最长 120s,
                        # 直接 await 会冻整个 event loop。其他用户 chat、helper、HTTP 都暂停。
                        # 包 to_thread 让阻塞落到 threadpool。
                        # 2026-05-09 Patch 15: 用 tool_registry 里同一 _TTS_SEMAPHORE 限并发,
                        # 跨调用点(主自动语音 / model 主动调 tts 工具)共享排队,防 OmniVoice
                        # 模型多份占爆显存/内存。
                        from app.llm.tools.registry import _async_semaphore, _GPU_SEMAPHORE as _shared_gpu_sem
                        from app.llm.tools.registry import _TTS_SEMAPHORE as _shared_tts_sem
                        from app.llm.tools.registry import tts_persona_guard as _tts_persona_guard
                        _guard_ok, _guard_reason = await _tts_persona_guard(
                            speak_text,
                            purpose="round3/final voice reply",
                        )
                        debug.log("voice.persona_guard", f"allow={_guard_ok} reason={_guard_reason}")
                        if not _guard_ok:
                            decision.use_voice = False
                            debug.log("voice.persona_guard.refused", _guard_reason or "persona refused voice TTS")
                            raise RuntimeError("persona_guard_refused_tts")
                        from app.llm.tools.tts_bridge import tts_clone
                        _tts_func = tts_clone if _voice_ref_audio else tts_design
                        _tts_kwargs = {
                            "language": _tts_lang,
                            "output": voice_path,
                            "timeout": 120,
                            "cwd": workspace_dir,
                        }
                        if _voice_ref_audio:
                            _tts_kwargs["ref_audio"] = _voice_ref_audio
                        else:
                            if not voice_instruct:
                                decision.use_voice = False
                                debug.log("voice.profile_missing", "skip round3 voice reply: active persona has no voice profile")
                                raise RuntimeError("TTS voice profile is not configured for the active persona")
                            _tts_kwargs["instruct"] = voice_instruct
                        async with _async_semaphore(_shared_gpu_sem):
                            async with _async_semaphore(_shared_tts_sem):
                                r = await asyncio.to_thread(
                                    _tts_func,
                                    speak_text,
                                    **_tts_kwargs,
                                )
                        # 2026-05-09 Patch 11: 不论 ok 与否先把 .tmp 文件交给后续逻辑;
                        # ok 时原子换名;不 ok 时清掉 .tmp 残骸。
                        #
                        # 2026-05-16 重构 (实测 trace 61167a31 atomic rename failed):
                        # OmniVoice 不可控 — 即使传 output=voice_path_tmp 也常忽略, 自己写到
                        # F:\chatbot\ominvioce\_tts_out_N.wav. 我们用 .part.wav 中转 (copy2 +
                        # os.replace) 走两步, 但实测某些情况 voice_path_tmp 在 os.replace
                        # 那一刻消失了 ([WinError 2]). 不可靠.
                        # 新策略: 跳过 .part.wav 中转, 直接验证 actual_path → shutil.move 到
                        # voice_path. shutil.move 支持跨盘 (内部 copy+delete), 单次操作.
                        if r.ok and r.paths:
                            actual_path = r.paths[0]
                            # 2026-05-17 Round 14j: OmniVoice 可能返**相对路径**
                            # 实测 trace fd13c5e5: actual_path='_voice_fd13c5e5_xxx.wav' 没目录前缀,
                            # os.path.isfile 用 cwd 解析失败 → 误报 "file not found".
                            # 尝试几个可能位置: workspace_dir / OmniVoice 子进程 cwd / cwd.
                            if actual_path and not os.path.isabs(actual_path):
                                _candidates = [
                                    os.path.join(workspace_dir, actual_path),
                                    actual_path,  # cwd 解析 (兜底)
                                ]
                                # 2026-05-17 Round 14k: 直接 import tts_bridge._OMNI_DIR
                                # (不是 settings 配置项, 是 module 常量)
                                try:
                                    from app.llm.tools.tts_bridge import _OMNI_DIR as _ov_dir
                                    _candidates.append(os.path.join(str(_ov_dir), actual_path))
                                except Exception:
                                    pass
                                _resolved = None
                                for _c in _candidates:
                                    if os.path.isfile(_c):
                                        _resolved = _c
                                        break
                                if _resolved:
                                    actual_path = _resolved
                                else:
                                    # 诊断 log: 列所有候选 + 是否存在, 看下次能定位
                                    debug.log(
                                        "voice.path_resolve_failed",
                                        f"actual_path={r.paths[0]!r} not found in any of: "
                                        + "; ".join(
                                            f"{_c}={'OK' if os.path.exists(_c) else 'NO'}"
                                            for _c in _candidates
                                        ),
                                    )
                            
                            if not actual_path or not os.path.isfile(actual_path):
                                r.ok = False
                                r.error = (
                                    f"TTS reported success but file not found: "
                                    f"actual_path={r.paths[0]!r} "
                                    f"voice_path_tmp_exists={os.path.exists(voice_path_tmp)} "
                                    f"voice_path_exists={os.path.exists(voice_path)}"
                                )
                            elif os.path.abspath(actual_path) == os.path.abspath(voice_path):
                                # 已就位 (OmniVoice 听了 output=voice_path 的情况)
                                pass
                            else:
                                # OmniVoice 写到了别的地方 → 一步搬到 voice_path
                                try:
                                    shutil.move(actual_path, voice_path)
                                except OSError as _me:
                                    log.warning("voice move failed: %s", _me)
                                    r.ok = False
                                    r.error = (
                                        f"move failed: {actual_path} → {voice_path}: {_me} "
                                        f"(src_exists={os.path.isfile(actual_path)})"
                                    )
                        # 清理可能残留的 .part.wav (旧路径残骸; 新路径不应留)
                        try:
                            if os.path.exists(voice_path_tmp):
                                os.unlink(voice_path_tmp)
                        except OSError:
                            pass
                        if r.ok and r.paths:
                            voice_url = f"/v1/chat/files/{req.archive_id}/{req.group_id}/{voice_fname}"
                            voice_reply_file = (voice_fname, voice_url, voice_path)
                            voice_suppress_text = True
                            debug.log("voice.reply",
                                      f"voice reply generated: {voice_fname} "
                                      f"({r.durations[0]:.1f}s, lang={_tts_lang or 'auto'}, "
                                      f"mode={'clone' if _voice_ref_audio else 'design'})")
                            # 2026-05-09 Patch 20 + 2026-05-10 Patch 81: 清理旧 _voice_*.wav 防累积爆磁盘。
                            # Patch 7 改成唯一文件名后,每条语音都留下一个 .wav。
                            # 保留最近 5 个(覆盖用户回放最近几条的需求),其余按 mtime
                            # 删掉。同时清理孤儿 .part.wav(Patch 81 失败留的半截文件)。
                            # 失败静默,不影响主流程。
                            try:
                                _voice_files = []
                                _orphan_tmps = []
                                for _f in os.listdir(workspace_dir):
                                    if _f.startswith("_voice_"):
                                        _fp = os.path.join(workspace_dir, _f)
                                        if _f.endswith(".part.wav"):
                                            # P81: .part.wav 是未完成产物,5 min 前视为孤儿
                                            # (优先匹配 .part.wav,因为它也以 .wav 结尾)
                                            try:
                                                if _vt.time() - os.path.getmtime(_fp) > 300:
                                                    _orphan_tmps.append(_fp)
                                            except OSError:
                                                pass
                                        elif _f.endswith(".wav.tmp"):
                                            # 兼容历史 .wav.tmp 命名(P11 旧路径,P81 已改),
                                            # 偶遇老文件清理
                                            try:
                                                if _vt.time() - os.path.getmtime(_fp) > 300:
                                                    _orphan_tmps.append(_fp)
                                            except OSError:
                                                pass
                                        elif _f.endswith(".wav"):
                                            try:
                                                _voice_files.append((os.path.getmtime(_fp), _fp))
                                            except OSError:
                                                pass
                                _voice_files.sort(reverse=True)  # 最新在前
                                _to_delete = [fp for _mt, fp in _voice_files[5:]] + _orphan_tmps
                                for _fp in _to_delete:
                                    try:
                                        os.unlink(_fp)
                                    except OSError:
                                        pass
                                if _to_delete:
                                    debug.log(
                                        "voice.prune",
                                        f"pruned {len(_voice_files[5:])} old .wav + "
                                        f"{len(_orphan_tmps)} orphan .part.wav/.tmp",
                                    )
                            except OSError as _pe:
                                log.debug("voice prune skipped: %s", _pe)
                        else:
                            log.warning("voice TTS generation failed: %s", r.error)
                            # 2026-05-16: error 为空时, 输出诊断信息让排查不抓瞎.
                            # 实测 trace ca7db44d: r.ok=False 但 r.error="", 完全无从下手.
                            _err_info = r.error[:200] if r.error else (
                                f"(no error msg) r.ok={r.ok} paths={r.paths} "
                                f"durations={r.durations} text_len={len(speak_text)}"
                            )
                            debug.log("voice.tts_failed",
                                      f"TTS failed for trace={_trace_prefix}: {_err_info}")
        except Exception as _voice_e:
            log.warning("voice decision/generation failed gracefully: %s", _voice_e)
            # 2026-05-16: 这层 except 之前也只有 log.warning, debug log 看不到任何线索.
            # 加 debug log 带 traceback 供排查.
            import traceback as _voice_tb
            debug.log(
                "voice.exception",
                f"voice flow exception: {_voice_e!r}; "
                f"tb_tail: {_voice_tb.format_exc()[-300:]}",
            )

        await debug.report()

        # Bug 5 修:abort 时 final_text 可能是空字符串(0 token 流出)。
        # 直接写空 assistant 到 hot memory 会让下一轮 bot 看历史时以为
        # "我上次什么都没回应" → 失忆/否认/胡说。trace 96071c40 的
        # "其实根本没搭"就是这个 bug 的下游结果。
        # 2026-05-03 修:让 final_text **保持空,UI 不显示任何文字**(用户感受
        # 一致),失忆问题改由下面的 stored_assistant 里的 <bot_log> 标签解决——
        # 标签只让模型看到,UI 不展示,既不打扰用户又给下次 bot 提供锚点。
        #
        # was_aborted 同时反映 Round 2 / Round 3 的 abort 状态:
        #   - r2_was_aborted: Round 2 期间 abort 过(旧 was_aborted 拿不到这个)
        #   - r3_new_abort:  Round 3 流式期间到达的新 abort
        # 任一为真都视为"被打断了",但用户屏幕上不再看到占位文本。
        r3_new_abort = abort_ch.gen > round3_start_gen
        was_aborted = r2_was_aborted or r3_new_abort
        if not final_text.strip():
            if was_aborted:
                # 静默 — 不回填可见占位文本。bot_log 会承接锚点责任。
                debug.log(
                    "round3.abort.silent",
                    "abort 路径:final_text 留空(UI 不显示文字),"
                    "下一轮 bot 通过 <bot_log> 标签知晓上次被打断",
                )
            else:
                # 不是 abort 但也空 — 罕见,可能是 model 直接返回空
                final_text = "（回复未生成）"
                debug.log("round3.empty.placeholder", "non-abort empty response")

        # #18 修:abort 时给前端发可视标记,让 UI 显示"已停止"
        if was_aborted:
            yield "abort_marker", {"trace_id": trace_id}

        # #16 修:把"本次实际做了什么"持久化到 hot memory 的 assistant 消息里
        # 用 <bot_log> 标签包,UI 不会展示(token 流已经发完),但下一轮 Round 3 加载
        # hot 时会看到——下次用户问"你刚做了啥""还在吗"就有了真实依据,不会撒谎。
        # 解决 trace 96071c40 的根本问题:bot 上次明明搭了 RL env,下一轮却说"没搭过"。
        # 2026-05-03 修:final_text 为空时(abort 静默)也要保留 <bot_log>,这是
        # 下一轮 bot 知晓"上次被打断"的唯一可靠锚点。stored_assistant 不再 strip
        # 前导换行(<bot_log> 自身有头/尾标签,模型 parse 时不依赖前导空白)。
        # 2026-05-03:为 bot_log 收集 helper 状态(用户追问"做了多少"时关键)
        _helper_status_for_botlog: dict[str, dict] = {}
        if workspace_dir:
            try:
                _bl_active, _bl_completed = await _scan_active_helpers(
                    trace_id=trace_id,
                    workspace_dir=workspace_dir,
                    log=log,
                    completed_since=_orch_wall_start,
                )
                # 区分:active(running)/ completed(done)/ aborted(was_aborted 时还在 active)
                for tid in _bl_active:
                    _helper_status_for_botlog[tid] = {
                        "status": "aborted" if was_aborted else "running",
                    }
                for tid in _bl_completed:
                    if tid not in _helper_status_for_botlog:
                        _helper_status_for_botlog[tid] = {"status": "done"}
            except Exception:
                pass  # 失败不影响 bot_log,degraded gracefully

        bot_log_payload = _build_bot_log(
            plan, generated_files, complexity, was_aborted,
            promoted_to_main=promoted_to_main_total,
            helper_status=_helper_status_for_botlog,
            internal_note=getattr(plan, "internal_note", "") if plan else "",
        )
        if bot_log_payload:
            if final_text:
                stored_assistant = final_text + f"\n\n<bot_log>{bot_log_payload}</bot_log>"
            else:
                # 静默 abort 路径 — 只存 bot_log,不带前导换行
                stored_assistant = f"<bot_log>{bot_log_payload}</bot_log>"
        else:
            stored_assistant = final_text

        done_payload: dict = {
            "tendencies": tendency_obj.tendencies,
            "trace_id": trace_id,
        }
        # ── 语音回复 ──
        if voice_reply_file:
            voice_reply_fname = voice_reply_file[0]  # 文件名,给 bridge 区分语音条 vs 文件
            done_payload["voice_reply"] = True
            done_payload["voice_reply_file"] = voice_reply_fname
            done_payload["_suppress_text"] = voice_suppress_text
            # voice_reply_file 也加入 files 列表
            if not generated_files:
                generated_files = []
            generated_files.append(voice_reply_file)

        if generated_files:
            # 2026-05-02 修:napcat 收到 file.name 时直接用作上传文件名,
            # Windows 文件名不能含 `/`(非法字符)。子目录里的文件如
            # `compress/compression_paper.docx` 用 basename "compression_paper.docx"
            # 即可——url/local_path 保留完整相对路径供下载和服务端找文件。
            # 实测 trace b00c015a:14 个 deliverable 全推送但 napcat 收不到,
            # 因为 name 里含 "/"。
            #
            # 2026-05-02 part6 复盘:part5 这里手滑写成 `_os_local.basename`(os 没这函数),
            # 导致整个 orchestrate 抛 AttributeError → done event 失败 → maintenance 没跑
            # → 上一轮回复没存进 hot 记忆 → 用户下一轮问"你做了什么"主线程啥也不知道。
            # 必须是 os.path.basename,不是 os.basename。
            #
            # 2026-05-02 part6 增强:外层加 try/except 兜底 — 即使 file 构造逻辑出 bug,
            # 也不能让整个 orchestrate 失败到错过 maintenance(hot/gm 持久化)。
            # 拿到 fallback 的 file list 至少能让下载 url 能用,maintenance 能跑。
            #
            # 2026-05-10 Patch 58:推送给用户的 displayed name 去掉内部 task_id 前缀。
            # 主区文件名(URL 用)仍带前缀以保持内部交互稳定;但 napcat 收到的 name
            # 字段是用户在 QQ 群看到的文件名,应该干净 — 不暴露智能体内部命名结构。
            # 映射来源:_copy_results_to_main / promote_to_main 时累积写入的
            # `.helpers_displayed_name.json` metadata 文件。
            _displayed_remap = _load_displayed_name_remap_for_delivery(
                main_workspace_dir,
                workspace_dir,
            )
            try:
                done_payload["files"] = [
                    {
                        # P58: name 字段用去前缀的用户友好名(若有 mapping),否则原名
                        "name": _displayed_remap.get(
                            os.path.basename(fname), os.path.basename(fname),
                        ),
                        "url": url,
                        "local_path": local_path,
                        "rel_path": fname,  # 保留原始相对路径(供调试 / 客户端兼容)
                    }
                    for fname, url, local_path in generated_files
                ]
            except Exception as _e_files:
                log.exception("[%s] done.files build failed; falling back to raw fname", trace_id)
                debug.error(f"done.files build failed: {type(_e_files).__name__}; using raw fname fallback")
                done_payload["files"] = [
                    {"name": fname, "url": url, "local_path": local_path}
                    for fname, url, local_path in generated_files
                ]

        # ── 2026-05-02 part7:暂停快照决策点 ──
        # 用户调 /v1/chat/abort 触发 abort_event,语义现在是"暂停",不是"扔掉":
        #   - helper 已经走 forced finalize 出报告(per-helper abort 由 shared abort 桥接 set)
        #   - workspace 保留(磁盘文件不被 cancel 影响)
        #   - 主线程把当前进度(round2 plan / round3 部分文本 / 活跃 helper 列表)写到磁盘,
        #     供下次同 (archive, group, user) 进 chat 时读取
        #
        # 正常完成路径(无 abort)→ 2026-05-02 part12 Bug B 修复:
        #   不再无脑 clear_pause。先扫 active helper:
        #     - 有 → 主动协作中断让它们 forced finalize 出报告 + save_pause(下次能 resume)
        #     - 没有 → clear_pause(下次 chat 不带"上次"包袱)
        #   trace 74b1295b 实测主进程 round3 done 后 helper rdh_v2 仍在跑(iter 118+),
        #   被 pause_state.cleared 误清后无人管,工作丢失。
        try:
            if abort_event is not None and abort_event.is_set():
                debug.log(
                    "pause_state.save.start",
                    "abort detected; collecting pause snapshot before yield 'done'",
                )
                await _collect_and_save_pause_snapshot(
                    req=req, trace_id=trace_id,
                    abort_event=abort_event,
                    plan=plan,
                    round3_partial_text=stored_assistant or "",
                    workspace_dir=workspace_dir,
                    debug=debug,
                    log=log,
                )
            else:
                # 正常完成 — 先看是否还有 helper 在跑
                active_by_tid, completed_by_tid = await _scan_active_helpers(
                    trace_id=trace_id, workspace_dir=workspace_dir, log=log,
                )
                if active_by_tid:
                    # 有还在跑的 helper → 协作中断让它们 finalize,然后保存 pause_state
                    debug.log(
                        "pause_state.active_helpers_detected",
                        f"normal completion but {len(active_by_tid)} active helper(s) still "
                        f"running ({list(active_by_tid.keys())}); requesting cooperative "
                        f"finalize + saving pause snapshot for resume next chat",
                    )
                    sent = await _request_active_helpers_finalize(
                        trace_id=trace_id, active_helpers=active_by_tid, debug=debug, log=log,
                    )
                    # 2026-05-03 Bug 3 修:sent==0 时不再无意义 sleep 1.5s。
                    # 之前 Bug 2 让幽灵 helper 进入 active_by_tid 但 proc_id 全是 None,
                    # _request_active_helpers_finalize 必返回 0,然后这里还是 sleep 1.5s,
                    # 用户每次正常完成都白等 1.5s。Bug 2 修后正常情况 sent==0 直接通过;
                    # sent>0 时按 helper 数动态选等待时长。
                    if sent == 0:
                        debug.log(
                            "pause_state.active_helpers_finalize",
                            f"no real helpers to signal (sent=0,跳过等待);"
                            f"active_by_tid={list(active_by_tid.keys())} all proc_id=None",
                        )
                    else:
                        _wait = 1.5 if sent <= 2 else 3.0
                        debug.log(
                            "pause_state.active_helpers_finalize",
                            f"cooperative abort signals sent to {sent} helper(s); "
                            f"waiting {_wait}s for them to write .helper_summary.txt",
                        )
                        try:
                            await asyncio.sleep(_wait)
                        except asyncio.CancelledError:
                            pass
                    # 重新扫一次拿最新 .helper_summary.txt 内容
                    active_by_tid_v2, completed_by_tid_v2 = await _scan_active_helpers(
                        trace_id=trace_id, workspace_dir=workspace_dir, log=log,
                    )
                    # 这条路径下 active_helpers 全标 interrupted=true
                    for h in active_by_tid_v2.values():
                        h["interrupted"] = True
                    # merge:已 finalize 的 helper 报告 excerpt 可能在 v2 里更全
                    for tid, h in active_by_tid_v2.items():
                        if not h.get("report_excerpt") and tid in active_by_tid:
                            old_excerpt = active_by_tid[tid].get("report_excerpt") or ""
                            if old_excerpt:
                                h["report_excerpt"] = old_excerpt
                    # 写 pause_state(走和 abort 路径一致的 merge 逻辑保护旧数据)
                    plan_dict = None
                    if plan is not None:
                        try:
                            plan_dict = plan.model_dump()
                        except Exception:
                            try:
                                plan_dict = dict(plan.__dict__)
                            except Exception:
                                plan_dict = None
                    # 合并旧 snapshot 不在本次 scan 中的 task_id
                    try:
                        old_snapshot = await _pause_state.load_pause(
                            archive_id=req.archive_id,
                            group_id=req.group_id, user_id=req.user_id,
                        )
                        if old_snapshot:
                            new_active_tids = set(active_by_tid_v2.keys())
                            new_completed_tids = set(completed_by_tid_v2.keys())
                            for h in (old_snapshot.get("active_helpers") or []):
                                tid = h.get("task_id")
                                if tid and tid not in new_active_tids and tid not in new_completed_tids:
                                    active_by_tid_v2[tid] = h
                            for h in (old_snapshot.get("completed_helpers") or []):
                                tid = h.get("task_id")
                                if tid and tid not in new_active_tids and tid not in new_completed_tids:
                                    completed_by_tid_v2[tid] = h
                    except Exception:
                        log.exception(
                            "[%s] pause_state load_old (normal-with-active-helpers) failed",
                            trace_id,
                        )
                    try:
                        await _pause_state.save_pause(
                            archive_id=req.archive_id,
                            group_id=req.group_id,
                            user_id=req.user_id,
                            trace_id=trace_id,
                            user_message=req.message,
                            round2_plan=plan_dict,
                            round3_partial_text=stored_assistant or "",
                            active_helpers=list(active_by_tid_v2.values()),
                            completed_helpers=list(completed_by_tid_v2.values()),
                        )
                        debug.log(
                            "pause_state.saved_with_active_helpers",
                            f"saved pause snapshot: {len(active_by_tid_v2)} active "
                            f"+ {len(completed_by_tid_v2)} completed helper(s); "
                            f"next chat can resume",
                        )
                    except Exception:
                        log.exception(
                            "[%s] pause_state save (normal-with-active-helpers) failed",
                            trace_id,
                        )
                else:
                    # 没有 active helper → 正常 clear
                    cleared = await _pause_state.clear_pause(
                        archive_id=req.archive_id,
                        group_id=req.group_id, user_id=req.user_id,
                    )
                    if cleared:
                        debug.log(
                            "pause_state.cleared",
                            "normal completion; pause snapshot (if any) removed",
                        )
        except Exception as _e_pause:
            # 任何 pause_state IO 错误都不应该让 done 事件失败 — 原路径优先
            log.exception("[%s] pause_state save/clear failed (non-fatal)", trace_id)
            debug.error(f"pause_state save/clear: {type(_e_pause).__name__}: {_e_pause}")

        yield "done", done_payload

        # ── 4. 后台维护：同步段仅做必要的 hot+gm 写入（亚毫秒），
        #    narration/group_events/压缩 全部 fire-and-forget 到后台 task ──
        # 2026-05-02 part6:整段独立 try。maintenance 内部各 await 已经各自 try-except 兜底
        # (hot.append / gm.append 都有 fallback jsonl),但**这一层**再加一道防线:
        # 万一 maintenance 顶层抛了未预期的错(比如 import 失败 / DB 连接断),
        # 至少不影响 user_released 信号、不阻断 finally 块的 group 锁释放。
        # done event 已经发出了,用户已经看到回复——后台失败应该静默 log 而非整个 chat 报错。
        yield "progress", _progress_payload("updating_memory", "maintaining", "正在更新记忆")
        debug.section("MAINTENANCE — sync hot writes, bg narration+compression")
        try:
            await _post_response_maintenance(
                req=req,
                user_message=req.message,
                assistant_message=stored_assistant,  # #16: 含 <bot_log> 标签的版本
                tendencies=tendency_obj.tendencies,
                plan=plan,
                trace_id=trace_id,
                generated_files=_gen_files_2tuple,
                workspace_dir=workspace_dir,
                progress_messages=progress_log,
                finalize_and_compress=_bg_finalize_and_compress,
                debug=debug,
            )
        except Exception as _e_mnt:
            log.exception("[%s] maintenance top-level failed; chat continues", trace_id)
            debug.error(f"maintenance top-level: {type(_e_mnt).__name__}: {_e_mnt}")
            # 不 re-raise — 用户的回复已经发出去了,记忆写入丢失影响下一轮但比整个 chat 失败强

    except Exception as e:
        log.exception("[%s] orchestrate failed", trace_id)
        debug.error(f"orchestrate failed: {type(e).__name__}: {e}")
        yield "error", {"message": f"处理失败：{type(e).__name__}"}
        return  # 异常后不发 complete，bridge 靠 error 事件判断失败

    finally:
        # 2026-05-10 Patch 55 + Patch 59: chat 回合彻底结束 → cancel 整棵 helper 树
        # 用户原话:"主进程结束后向用户回复,此时所有子进程都应该结束,没有意义了"
        # P55 用 owner 过滤,但 P40 派的 paired sub-helper owner
        # 是 helper:{trace_id}:{parent},不是 main_owner → P55 漏掉。
        # P59:改用 trace_id 过滤,cancel 整棵树。
        # 不存在合理的"主进程结束后子进程继续跑"场景:
        #   - chat 已 yield 'done',用户已拿到回复
        #   - 任何还在跑的 helper 都没人看
        #   - 用户下次问相关任务时,主进程通过 resume=True 重启 task(workspace 仍在)
        try:
            from app.core.core_processes import proc_registry
            if 'trace_id' in locals() and trace_id:
                _n = await proc_registry().cancel_all_helpers_in_trace(trace_id)
                if _n > 0:
                    debug.log(
                        "orchestrate.helpers_cleanup",
                        f"chat 回合结束,P59 按 trace_id cancel 了 {_n} 个 helper(整棵树)",
                    )
        except Exception:
            # cleanup 失败不阻断 finally 后续(workspace unregister / 锁释放)
            pass

        if workspace_dir:
            from app.core.bg_tasks import schedule
            schedule(_delayed_workspace_unregister(workspace_dir, group_key),
                     name="orch.delayed_unregister")
        # 2026-05-02 part12 Bug C:reset user_lang ContextVar
        try:
            if '_user_lang_token' in locals() and _user_lang_token is not None:
                from app.llm.tools.delegate import reset_current_user_lang as _reset_user_lang
                _reset_user_lang(_user_lang_token)
        except Exception:
            pass
        # 2026-05-10 Patch 83:reset 人设守卫 ContextVar
        try:
            if '_persona_excerpt_token' in locals() and _persona_excerpt_token is not None:
                from app.llm.tools.delegate import reset_current_persona_excerpt as _rp
                _rp(_persona_excerpt_token)
            if '_voice_instruct_token' in locals() and _voice_instruct_token is not None:
                from app.llm.tools.registry import reset_current_voice_instruct as _rv
                _rv(_voice_instruct_token)
            if '_tts_guard_token' in locals() and _tts_guard_token is not None:
                from app.llm.tools.registry import reset_current_tts_guard_context as _rtg
                _rtg(_tts_guard_token)
            if '_voice_ref_token' in locals() and _voice_ref_token is not None:
                from app.llm.tools.registry import reset_current_voice_ref_audio as _rr
                _rr(_voice_ref_token)
            if '_user_msg_token' in locals() and _user_msg_token is not None:
                from app.llm.tools.delegate import reset_current_user_message as _ru
                _ru(_user_msg_token)
        except Exception:
            pass

    debug.log("orchestrate.complete", "all done; releasing group lock soon")
    await debug.report()
    # 2026-05-02 part9 #16:complete event 带各阶段累计耗时
    # 前端可显示"Round1 1.2s · Round2 45s · Round3 8s · 总 65s"等
    # 不存在的阶段(比如 easy 路径没 round2)不出现在字典里
    _mark("total")
    yield "complete", {"trace_id": trace_id, "timing": _timing}
