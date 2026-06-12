"""Delegate wait loop, kill, and helper-only compatibility handlers."""
from __future__ import annotations

import re


def _normalize_guard_result(result) -> tuple[bool, str]:
    """Normalize guard output to the only runtime decision: allow/block + reason.

    Older callers may still pass split/kind/framework fields. They are ignored
    here: symbolic checks provide facts to the guard, and only the guard's
    should_act verdict is executed.

    守卫运行时只读取是否允许和自由理由；旧结构化字段不参与程序决策。
    """
    if not isinstance(result, tuple):
        return True, ""
    if len(result) >= 5:
        should_act, veto_reason = result[:2]
    elif len(result) == 4:
        should_act, veto_reason = result[:2]
    elif len(result) == 3:
        should_act, veto_reason = result[:2]
    elif len(result) == 2:
        should_act, veto_reason = result
    elif len(result) == 1:
        should_act = bool(result[0])
        veto_reason = ""
    else:
        should_act = True
        veto_reason = ""
    return bool(should_act), str(veto_reason or "")


def _zero_result_wait_extension_seconds(wait_window_sec: float) -> float:
    """Return the one-time extension when helpers are alive but no report exists yet."""
    try:
        window = float(wait_window_sec)
    except (TypeError, ValueError):
        window = 90.0
    return min(max(window * 2.0, 180.0), 300.0)


def _zero_result_final_grace_seconds(wait_window_sec: float) -> float:
    """Return a bounded final grace period after a zero-result wait extension."""
    try:
        window = float(wait_window_sec)
    except (TypeError, ValueError):
        window = 90.0
    return min(max(window, 90.0), 120.0)


def _sync_delegate_action_globals() -> None:
    from app.llm.tools import delegate as _delegate
    from app.llm.tools import delegate_actions as _actions
    globals().update({
        name: value
        for module in (_delegate, _actions)
        for name, value in vars(module).items()
        if not name.startswith("__") and name not in {
            "_dynamic_wait_loop",
            "_handle_main_kill_helper",
            "handle_spawn_helper",
            "handle_wait_helper",
        }
    })


def _kind_mode_guidance(rec: dict) -> tuple[str, str, str]:
    """Return display text, follow-up mode value, and recovery-plan guidance."""
    suggested_mode = str(rec.get("observed_helper_mode_name") or rec.get("suggested_mode") or "").strip().lower()
    mode_reason = str(rec.get("mode_reason") or "").strip()
    if suggested_mode in {"easy", "hard"}:
        display = f", observed_helper_mode_name={suggested_mode!r}"
        if mode_reason:
            display += f", mode_reason={mode_reason}"
        plan = (
            f"Observed compatible mode name: {suggested_mode!r}. Compare it with the active task evidence "
            "before retrying; mode changes resource discipline, not tool access."
        )
        return display, suggested_mode, plan
    return (
        "",
        "preserve_original_mode",
        "Preserve the original mode unless root-cause review shows the same kind needs a stricter hard retry; mode changes resource discipline, not tool access.",
    )


def _compact_string_list_field(raw, *, max_items: int = 40, max_chars_each: int = 500) -> list[str]:
    """Normalize a model-visible task list field without changing its meaning.

    保留任务字段含义，仅做列表化和长度收敛。
    """
    if raw in (None, "", [], {}):
        return []
    if isinstance(raw, str):
        values = [line.strip(" -\t") for line in raw.splitlines()]
    elif isinstance(raw, dict):
        values = [f"{k}: {v}" for k, v in raw.items()]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(x).strip() for x in raw]
    else:
        values = [str(raw).strip()]
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        if len(value) > max_chars_each:
            value = value[:max_chars_each].rstrip() + "..."
        cleaned.append(value)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _preserve_task_envelope_for_retry(
    spec: dict,
    *,
    kind: str | None = None,
    mode: str | None = None,
    framework_placeholder: str | None = None,
    prompt_prefix: str = "",
) -> dict:
    """Build a retry template that preserves the original helper envelope.

    The guard should not make the main model rediscover task fields after an
    intervention. This returns a concrete starting shape while leaving the main
    model responsible for the next delegation decision.

    守卫只提供保留字段的重派模板；主进程仍负责最终派发决策。
    """
    original = spec if isinstance(spec, dict) else {}
    task_id = str(original.get("task_id") or "").strip() or "<same_task_id>"
    original_prompt = str(original.get("prompt") or "").strip()
    retry = {
        "task_id": task_id,
        "kind": kind or str(original.get("kind") or "code").strip().lower() or "code",
        "mode": mode or str(original.get("mode") or "easy").strip().lower() or "easy",
        "resume": bool(original.get("resume", False)),
        "framework": (
            framework_placeholder
            if framework_placeholder is not None
            else str(original.get("framework") or "")
        ),
        "dispatch_reason": str(
            original.get("dispatch_reason")
            or original.get("routing_reason")
            or original.get("delegation_reason")
            or ""
        ).strip()[:1200],
        "input_files": _compact_string_list_field(
            original.get("input_files")
            or original.get("source_files")
            or original.get("transferred_files")
            or original.get("files"),
            max_items=60,
        ),
        "prompt": original_prompt,
        "expected_outputs": _compact_string_list_field(
            original.get("expected_outputs"),
            max_items=20,
        ),
        "acceptance_checks": _compact_string_list_field(
            original.get("acceptance_checks") or original.get("checks"),
            max_items=20,
        ),
    }
    if prompt_prefix:
        retry["prompt"] = (prompt_prefix.rstrip() + "\n\n" + original_prompt).strip()
    if original.get("fork_from"):
        retry["fork_from"] = str(original.get("fork_from") or "").strip()
    if not retry["prompt"]:
        retry["prompt"] = (
            "Continue the same logical helper work from the original user goal. "
            "Keep the task bounded to this helper's declared kind, outputs, files, and acceptance checks."
        )
    return retry


async def _build_guard_intervention(
    guard_result,
    *,
    trace_id: str,
    cancel_helpers: bool,
    helper_specs: list[dict] | None = None,
) -> dict | None:
    """Convert a guard allow/block verdict into a model-visible payload.

    Symbolic checks and old structured fields are facts for the guard only. The
    runtime executes exactly one guard decision: should_act=false blocks this
    delegation, and should_act=true allows it.

    符号化检测只供守卫参考；运行时只执行守卫 allow/block 判决与自由理由。
    """
    _sync_delegate_action_globals()
    _sync_delegate_globals()

    if not isinstance(guard_result, tuple):
        return None
    _should_act, _guard_reason = _normalize_guard_result(guard_result)
    if _should_act is not False:
        return None

    _killed = 0
    if cancel_helpers:
        try:
            _killed = await proc_registry().cancel_all_helpers_in_trace(trace_id)
        except Exception:
            _killed = 0
    return {
        "ok": False,
        "error": "guard_blocked",
        "error_kind": "guard_blocked",
        "preflight_guard": False,
        "reason": _guard_reason,
        "killed_helpers": _killed,
        "instruction": (
            "The task-quality guard blocked this delegation. The reason is free-form guard feedback, not a structured program decision. "
            "Replan only after considering the stated facts; if the main thread believes the delegation is still correct, call delegate again with a clear dispatch_reason explaining why.\n\n"
            "守卫已拦截本次派发；理由为自由事实说明。主进程可根据事实重规划，或用 dispatch_reason 说明后重新派发。"
        ),
    }

async def _dynamic_wait_loop(
    initial_tasks: list[asyncio.Task],
    spawn_queue: asyncio.Queue,
    *,
    on_done=None,
    wait_window_sec: float | None = None,
    min_results_to_return: int | None = None,
    twin_map: dict[str, str] | None = None,
    main_owner: str = "",
    guard_task: asyncio.Task | None = None,
    helper_specs: list[dict] | None = None,
) -> list[dict] | dict:
    """等待 helper task 集合完成,支持 spawn_queue 中途加入新 task。

    设计:每次 wait 时同时监听 spawn_queue.get(),收到新 spawn 立即唤醒,
    把新 task 加入 pending 集合继续等。零轮询延迟。

    Args:
        initial_tasks: 初始 helper task 列表
        spawn_queue: 新 spawn 出的 helper task 会被 put 进来,格式 (task, task_id, proc_id)
        on_done: 每个 helper 完成时回调(result_dict, total_completed)
        wait_window_sec: (2026-05-02 加) 可选,等待窗口上限秒数。
            若指定且超过此时间还有 helper 未完成,**不等了直接返回**,
            未完成的 task 在 pending 集合里 (still 跑着,会被外层 timeout 处理或 reap)。
            None = 等所有 helper 完成 (旧行为,delegate spawn 默认值)。

            用途: 主线程要在 spawn 期间介入(查心跳 / kill stale helper),
            可以传一个较小的 wait_window_sec(如 90s),拿到中段状态后决定。

        min_results_to_return: (2026-05-03 优化 #1 加) 可选,最快 N 个 helper
            完成就立刻返回,不等剩下的。剩下的 helper 在 pending 集合里继续跑,
            主线程后续可以通过 processes.list 看状态,或调 wait_helper(task_id=...)
            继续等。**对处理 helper 完成时间分布失衡场景效果显著**:
            实测 trace 09ba132f arith 230s 完成但要等 bwt_fix 533s,主线程白等 5 分钟。
            None = 等所有(等于旧行为)。

        twin_map: hard-mode paired helper 的双向映射 task_id↔task_id。
            任一方 ok=true 时,系统自动 graceful-abort 另一方;loser 的 result
            会被改写 report + 加 race_lost_to 字段(LLM 看到不会误 resume)。
            None = 无配对。

        main_owner: (2026-05-08 加) 主线程的 ProcessRegistry owner ID。
            twin race-cancel 时通过 owner 反查 helper handle 调 abort_event.set()。

    Returns: 已完成的 helper 的 result list。如 wait_window_sec / min_results_to_return
        触发,返回结果中可能少于 initial_tasks + spawned tasks 总数 — 调用方需检查。
    """
    _sync_delegate_action_globals()

    _sync_delegate_globals()

    if twin_map is None:
        twin_map = {}
    pending: set[asyncio.Task] = set(initial_tasks)
    results: list[dict] = []

    import time as _t
    deadline = (_t.monotonic() + wait_window_sec) if wait_window_sec else None
    _deadline_extended = False  # 自适应延长仅一次
    _zero_result_final_grace_used = False

    while pending:
        # ── stuck 感知:有 helper 报告 stuck 时缩短 deadline,不再傻等 ──
        if deadline is not None and results:
            stuck_count = sum(1 for r in results if r.get("stuck"))
            if stuck_count > 0:
                new_deadline = _t.monotonic() + 60.0
                if new_deadline < deadline:
                    deadline = new_deadline
                    debug.log(
                        "delegate.wait_window.stuck_shorten",
                        f"stuck_count={stuck_count}, "
                        f"deadline shortened to +60s from now",
                    )
        # 计算剩余等待时间
        remaining = None
        if deadline is not None:
            remaining = deadline - _t.monotonic()
            if remaining <= 0:
                # ── 自适应延长:0 结果时延长一次窗口,避免白等 ──
                # 主模型常低估 coding helper 需要的时间(实测 wait=300s,
                # p8483_solve 需 974s)。白等 300s 后主线程拿不到任何结果,
                # 又会 spawn 新 helper,形成恶性循环。
                if len(results) == 0 and wait_window_sec and not _deadline_extended:
                    _extend = min(wait_window_sec, 300.0)
                    deadline = _t.monotonic() + _extend
                    _deadline_extended = True
                    debug.log(
                        "delegate.wait_window.extended",
                        f"0 results after {wait_window_sec}s; "
                        f"extending by {_extend:.0f}s (one-time) "
                        f"because helpers are still running",
                    )
                    continue
                if (
                    len(results) == 0
                    and wait_window_sec
                    and _deadline_extended
                    and not _zero_result_final_grace_used
                    and pending
                ):
                    _grace = _zero_result_final_grace_seconds(wait_window_sec)
                    deadline = _t.monotonic() + _grace
                    _zero_result_final_grace_used = True
                    debug.log(
                        "delegate.wait_window.zero_result_final_grace",
                        f"0 results after the extended wait window; granting {_grace:.0f}s final grace "
                        f"because {len(pending)} helper task(s) are still pending",
                    )
                    continue
                # wait_window 到了,不等了 — pending 留给调用方处理
                debug.log(
                    "delegate.wait_window.expired",
                    f"wait_window_sec={wait_window_sec} reached; "
                    f"{len(results)} done, {len(pending)} still running — "
                    f"returning partial results to main thread",
                )
                break
        # 创建 wakeup task 监听 spawn_queue,与 pending 一起 wait
        wakeup = asyncio.create_task(spawn_queue.get())
        # 2026-05-10 Patch 83: 守卫并入 wait set —— 守卫完成时立刻唤醒主循环
        _wait_set = pending | {wakeup}
        if guard_task is not None and not guard_task.done():
            _wait_set = _wait_set | {guard_task}
        try:
            done, _ = await asyncio.wait(
                _wait_set,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=remaining,  # None = 无限等;有 wait_window 时按剩余秒数
            )
        except BaseException:
            wakeup.cancel()
            raise

        # Guard verdict check. The guard now owns the hard block decision; old
        # split/kind/framework structured fields are ignored by runtime.
        #
        # 守卫只返回 allow/block + 自由理由；符号化事实不在这里直接执行。
        guard_task_local_ref = guard_task  # 备份引用,稍后从 done 移除
        if guard_task is not None and guard_task.done():
            try:
                _result = guard_task.result()
            except Exception as _gerr:
                _result = (True, f"guard_exception: {_gerr}")
            _guard_payload = await _build_guard_intervention(
                _result,
                trace_id=debug.current_trace_id() or "unknown",
                cancel_helpers=True,
                helper_specs=helper_specs,
            )
            if _guard_payload is not None:
                wakeup.cancel()
                for _t in pending:
                    if not _t.done():
                        _t.cancel()
                return _guard_payload

            debug.log("delegate.quality_guard.passed", "guard allowed delegation")
            guard_task = None
        # 不论否决还是通过,从 done 移除 guard_task(它不是 helper task)
        if guard_task_local_ref is not None:
            done.discard(guard_task_local_ref)
        if not done and guard_task_local_ref is not None and guard_task_local_ref.done():
            wakeup.cancel()
            try:
                await wakeup
            except (asyncio.CancelledError, BaseException):
                pass
            continue
        # asyncio.wait 超时返回 done={} — 退出循环让外层处理 pending
        if not done:
            wakeup.cancel()
            try:
                await wakeup
            except (asyncio.CancelledError, BaseException):
                pass
            # 2026-05-09 Patch 37: 自适应延长激活
            # 病因(trace 779bbcf0):asyncio.wait timeout 后直接 break,line 5164 的
            # `if remaining <= 0` 自适应延长分支永远不可达(因为已经被 break 拦走)。
            # 实测 18 次 wait_window timeout 全部 0 results,无一触发延长 → 主线程
            # 反复醒来 spawn(resume),浪费 LLM 调用。
            # 新行为:0 results 时给一次延长(min(wait_window_sec, 300)),helper 健康
            # 但慢的场景下避免主线程刷 LLM。延长仅一次,_deadline_extended flag 控。
            # 注意:有 results 时不延长(已有部分进展,正常返回让主线程整合)
            if (
                len(results) == 0
                and wait_window_sec
                and not _deadline_extended
                and pending  # 还有 helper 在跑
            ):
                _extend = _zero_result_wait_extension_seconds(wait_window_sec)
                deadline = _t.monotonic() + _extend
                _deadline_extended = True
                debug.log(
                    "delegate.wait_window.extended",
                    f"asyncio.wait timeout reached at {wait_window_sec}s with 0 results; "
                    f"extending by {_extend:.0f}s (one-time) "
                    f"because {len(pending)} helper(s) still running",
                )
                continue  # 重入 while 循环再等一段
            if (
                len(results) == 0
                and wait_window_sec
                and _deadline_extended
                and not _zero_result_final_grace_used
                and pending
            ):
                _grace = _zero_result_final_grace_seconds(wait_window_sec)
                deadline = _t.monotonic() + _grace
                _zero_result_final_grace_used = True
                debug.log(
                    "delegate.wait_window.zero_result_final_grace",
                    f"asyncio.wait timeout reached after extension with 0 results; "
                    f"granting {_grace:.0f}s final grace because {len(pending)} helper(s) still pending",
                )
                continue
            debug.log(
                "delegate.wait_window.timeout",
                f"asyncio.wait timeout reached at {wait_window_sec}s; "
                f"{len(results)} done, {len(pending)} pending",
            )
            break

        # 收集 spawn item(从 wakeup + 残留 queue)
        spawned_items: list[tuple] = []
        if wakeup in done:
            try:
                spawned_items.append(wakeup.result())
            except Exception:
                log.exception("wakeup task got exception (impossible normally)")
            done.discard(wakeup)
        else:
            wakeup.cancel()
            try:
                await wakeup
            except (asyncio.CancelledError, BaseException):
                pass

        # 处理完成的 helper
        for t in done:
            try:
                r = await t
            except asyncio.CancelledError:
                # 这是用户全局 abort 的正常路径(`/v1/chat/abort`):
                #   1. 用户调 abort → group_abort_event.set()
                #   2. 主线程 chat_with_tools_loop racing 机制 cancel 当前 tool dispatch
                #   3. handle_delegate 的 except CancelledError(line ~1648)cancel 所有 helper_tasks
                #   4. helper task 抛 CancelledError → 在这里被捕获
                # 也可能是其他罕见 cancel 来源(系统资源、运行时 bug 等)。
                # processes.kill(force=true) 已禁用,不会从那条路径来到这里。
                # 这里捕住,不让整个 batch 崩(其他 helper 还在跑)。
                log.info("delegate helper task got CancelledError "
                         "(likely user abort or system cancellation)")
                r = {
                    "task_id": "?",
                    "ok": False,
                    "interrupted": True,
                    "stuck": False,
                    "stuck_reason": "asyncio_cancelled",
                    "resumed_from": False,
                    "report": (
                        "(This helper was cancelled by asyncio before producing a final report. "
                        "This usually follows a user-level abort or system cancellation, so the main conversation "
                        "has normally ended and this report will not be consumed. Workspace files are preserved, "
                        "but resuming is usually not useful unless the main process explicitly continues the task.)\n\n"
                        "helper 被取消且未出最终报告；通常不需要续作。"
                    ),
                }
            except Exception as e:
                log.exception("delegate helper task crashed")
                r = {
                    "task_id": "?", "ok": False, "interrupted": False,
                    "stuck": False, "stuck_reason": "",
                    "resumed_from": False,
                    "report": f"helper task crashed: {type(e).__name__}: {e}",
                    "terminal_reason": "crashed",  # 2026-05-11 P2.1
                    "crash_type": type(e).__name__,
                }
            results.append(r)
            try:
                setattr(t, "_delegate_result_collected", True)
            except Exception:
                pass
            pending.discard(t)
            # 2026-05-08 final-helper twin race: 任一 ok=true 完成 → 杀另一个
            # (graceful: 设 abort_event,helper 跑完当前 tool 后做 forced finalize)。
            # 仅 ok=true 才 cancel, 失败/中断的不动(让另一个有机会替代)。
            _winner_tid = r.get("task_id") or ""
            if r.get("ok") and _winner_tid in twin_map:
                _loser_tid = twin_map[_winner_tid]
                try:
                    _h = await proc_registry().find_helper_by_task_id(
                        _loser_tid, owner=main_owner,
                    )
                    if _h is None:
                        _h = await proc_registry().find_helper_by_task_id(
                            _loser_tid, same_trace_as=main_owner,
                        )
                    if _h is not None and _h.helper_task is not None and not _h.helper_task.done():
                        if _h.abort_event is not None and not _h.abort_event.is_set():
                            _h.abort_event.set()
                            debug.log(
                                f"delegate.{_winner_tid}.twin_race_won",
                                f"twin {_loser_tid} graceful-aborted "
                                f"(winner={_winner_tid} ok=true; loser will "
                                f"forced-complete and be marked race_lost_to)",
                            )
                except Exception:
                    log.exception(
                        "twin race-cancel failed (winner=%s loser=%s); "
                        "loser continues running, no harm",
                        _winner_tid, twin_map.get(_winner_tid, "?"),
                    )
            # 反向: 若本次 r 是被 race-cancel 的 loser(twin 已 ok 完成),
            # 重写 report 让主线程明白这不是"自然中断需要 resume"——而是被赢家替代了。
            _this_tid = r.get("task_id") or ""
            if r.get("interrupted") and _this_tid in twin_map:
                _twin_tid = twin_map[_this_tid]
                _twin_won = any(
                    x.get("task_id") == _twin_tid and x.get("ok")
                    for x in results[:-1]  # results[:-1] 排除自己(刚 append)
                )
                if _twin_won:
                    r["report"] = (
                        f"[Hard-mode paired race loser: sibling task `{_twin_tid}` completed the same task successfully. "
                        f"This helper was gracefully cancelled. Do not resume this task; use `{_twin_tid}` outputs.]\n"
                        f"竞速兄弟任务已成功，本 helper 不需要续作。"
                    )
                    # 不再标 interrupted——LLM 看到 interrupted=true 容易触发 resume 反射
                    r["interrupted"] = False
                    r["race_lost_to"] = _twin_tid
            on_done_signal = None
            if on_done:
                try:
                    on_done_signal = on_done(r, len(results))
                except Exception:
                    pass

        # Drain queue 中可能在我们处理时累积的 item
        while not spawn_queue.empty():
            try:
                spawned_items.append(spawn_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        for item in spawned_items:
            new_task = item[0]
            pending.add(new_task)

        # 2026-05-03 优化 #1:min_results_to_return — 够 N 个就早退
        # 默认 1:只要任一 helper 完成就唤醒主线程,保证响应实时性。
        # on_done 回调可返回 "skip" 否决本次早退("还不够,继续等")。
        if (min_results_to_return is not None
                and len(results) >= min_results_to_return
                and pending):
            if on_done_signal != "skip":
                debug.log(
                    "delegate.min_results.early_return",
                    f"min_results_to_return={min_results_to_return} reached "
                    f"({len(results)} done); {len(pending)} still running — "
                    f"returning early to let main thread proceed",
                )
                break

    # 2026-05-10 Patch 55: 不再在 dispatcher 退出时 cancel helper。
    # 旧 P50 在这里 cancel 自动 paired/verify helper,但 dispatcher 退出 ≠ chat
    # 回合结束 — 主进程下次 tool call 可能调 wait_helper 等剩下的 helper。
    # 所有 helper 的最终 cancel 移到 orchestrator 的 finally 块(chat 回合彻底结束)。
    return results


async def _handle_main_kill_helper(args: dict) -> str:
    """主线程定向 kill 某个或多个 helper(by task_id / task_ids)。

    与 processes.kill 的区别:这个走 task_id(更直观,主线程只持有 task_id)。
    多用户隔离:只能 kill 与当前 trace_id 同会话的 helper(防止误杀别的用户的 helper)。

    2026-05-02 重构:**永远协作中断,不再支持 force=true**。
    helper 收到 abort 信号会输出当前进展报告再退出,工作区保留可 resume 续作。
    主线程要让 helper 沿新方向继续 → delegate(task_id=同, resume=true, prompt=新指示)。

    2026-05-05 kill gate:必须传 reason 字段,校验是否在四种允许条件内。

    2026-05-12 P30 新增:支持 task_ids 数组,一次 kill 多个 helper。
    实测教训(12:32:40 trace): 主线程一次性发出 7 个并行 kill call(每个含 task_id 单数),
    浪费 LLM iter + 调度开销。改成支持 task_ids=[...] 一次 call 搞定。
    兼容性: 同时支持 task_id (单数, 旧路径) 和 task_ids (数组, 新路径)。
    """
    _sync_delegate_action_globals()

    _sync_delegate_globals()

    # P30: 支持 task_ids 数组,优先级高于 task_id 单数
    task_ids_arg = args.get("task_ids")
    if task_ids_arg and isinstance(task_ids_arg, list):
        # 多个 task_id,循环 kill
        # reason 是共享的(批量 kill 通常同一原因, 如 content_deemed_useless)
        results = []
        for _tid in task_ids_arg:
            _tid = str(_tid).strip()
            if not _tid:
                continue
            _sub_args = {**args, "task_id": _tid}
            _sub_args.pop("task_ids", None)  # 避免递归
            _result_json = await _handle_main_kill_helper(_sub_args)
            try:
                results.append(json.loads(_result_json))
            except Exception:
                results.append({"task_id": _tid, "ok": False, "error": "internal parse error"})
        return json.dumps(
            {"ok": True, "action": "kill", "batch": True,
             "killed": len(results), "results": results,
             "note": (
                 f"Batch cooperative interruption was sent to {len(results)} helper(s). "
                 "Use the returned per-helper results to decide whether to resume, reuse outputs, or continue the main workflow.\n"
                 "已批量发送协作中断信号；根据各 helper 结果决定续作或继续主流程。"
             )},
            ensure_ascii=False,
        )

    task_id = str(args.get("task_id", "")).strip()
    if not task_id:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "kill requires task_id or task_ids.\n"
                    "kill 需要 task_id 或 task_ids。"
                ),
            },
            ensure_ascii=False,
        )

    # ── kill gate:校验 reason(2026-05-05: gate 已下沉到 ProcessRegistry.kill,
    # 但此处提前校验给出更友好的错误消息) ──
    # 2026-05-08: 拆成两步, 先白名单匹配(便宜), 找到 helper 后再做语义校验
    # (api_stall_emergency 要核对心跳)
    reason = str(args.get("reason", "")).strip()
    if not reason:
        return json.dumps(
            {"ok": False, "error": (
                "kill requires a reason code. Valid values: "
                f"{KILL_REASON_SELF_CANT_DO}, {KILL_REASON_SELF_DONE}, "
                f"{KILL_REASON_SIBLING_DONE}, {KILL_REASON_CONTENT_USELESS}, "
                f"{KILL_REASON_API_STALL}.\n"
                "kill 必须提供合法 reason。"
            )},
            ensure_ascii=False,
        )
    # 第一步: 白名单匹配(无 handle, 仅枚举值校验)
    allowed, gate_msg = validate_kill_reason(task_id, reason)
    if not allowed:
        return json.dumps({"ok": False, "error": gate_msg}, ensure_ascii=False)

    force_requested = bool(args.get("force", False))
    if force_requested:
        log.info(
            "delegate.kill_helper: force=true 被拒绝,降级为协作中断 (task_id=%s)",
            task_id,
        )
    owner = current_owner()  # 应该是 main:trace_id
    h = await proc_registry().find_helper_by_task_id(task_id, same_trace_as=owner)
    # 第二步: 如果找到了 helper, 做语义校验(如 api_stall 要心跳老化)
    if h is not None:
        allowed, gate_msg = validate_kill_reason(task_id, reason, helper_handle=h)
        if not allowed:
            return json.dumps({"ok": False, "error": gate_msg}, ensure_ascii=False)
    debug.log("delegate.kill_helper.gate", gate_msg)
    if h is None:
        # 幂等检查：可能已被前一次 kill 终止并 deregister
        if await proc_registry().was_recently_killed(task_id):
            return json.dumps(
                {"ok": True, "already_killed": True, "task_id": task_id,
                 "note": (
                     "This helper was already cooperatively interrupted and deregistered. "
                     "Use the current or next delegate report for its result instead of repeating kill.\n"
                     "helper 已被中断并注销，无需重复 kill。"
                 )},
                ensure_ascii=False,
            )
        # 2026-05-08 Fix(Bug 6): 自然完成 / 已 gone 的 helper 也走 ok=true。
        # 旧版返回 ok=false ERROR, 实测 LLM 完全忽略 ERROR 14 秒内连发 4 次 kill。
        # 把所有"helper 不在了"的子情况统一为 ok=true + 明确"无需再 kill",
        # 让 LLM 不再把这当作"待修复的失败"。
        if await proc_registry().was_recently_completed(task_id):
            return json.dumps(
                {"ok": True, "already_completed": True, "task_id": task_id,
                 "note": (
                     f"task_id={task_id!r} already completed naturally and its outputs were merged into the main workspace. "
                     "Continue from the existing outputs.\n"
                     "helper 已自然完成且产物已合并，直接基于现有产物继续。"
                  )},
                ensure_ascii=False,
            )
        return json.dumps(
            {"ok": True, "already_gone": True, "task_id": task_id,
             "note": (
                f"No active helper was found for task_id={task_id!r}. It may have completed, been interrupted, "
                "or belonged to another session. Treat this as already handled; do not repeat kill.\n"
                "未找到活跃 helper 时视为已处理，不要重复 kill。"
             )},
            ensure_ascii=False,
        )
    # 始终 force=False:协作中断,helper 自报告退出
    result = await proc_registry().kill(
        h.proc_id, requested_by=owner, reason=reason, force=False,
    )
    result["task_id"] = task_id

    # 2026-05-10 Patch 70: kill 返回强提示 resume,治"kill 后派新 task_id 重做"反模式
    # 病因(trace f973df3770544567):主线程 09:28 kill embed_charts(理由 content_deemed_useless),
    # 然后 1.5h 后(10:06)派 embed_final 做完全相同的任务。LLM 选了新 task_id 而非 resume,
    # 导致工作区重新 fork(70+ 文件)+ 重新做已完成的部分(图表 PNG embed_charts 已 promote 过)。
    # 修法:kill return 明确教模型"如果想继续做同任务,用 resume=true 同 task_id"。
    # 现有 hint 在 schema 文档里,但 LLM 实际 kill 时直接看 return,所以 return 里加更醒目。
    result["resume_hint"] = (
        f"You just cooperatively interrupted task_id `{task_id}`. If the same work boundary should continue, "
        f"resume it with `delegate(action='spawn', tasks=[{{task_id: '{task_id}', resume: true, "
        f"prompt: '<new focused direction>'}}])`. Keep the same task_id so the preserved workspace can be reused; "
        f"a new task_id starts from a fresh fork.\n\n"
        f"同一任务继续时用同 task_id + resume，不要换新 task_id。"
    )

    if force_requested:
        result["force_downgraded"] = True
        result["note"] = (
            "force=true was downgraded because helpers use cooperative interruption. "
            "The helper receives an interrupt signal and may produce a progress report before exiting. "
            "To continue with a new direction, resume the same task_id with resume=true and a focused prompt.\n\n"
            "force 已降级为协作中断；继续同一任务请用同 task_id 续作。"
        )
    return json.dumps(result, ensure_ascii=False)


async def handle_spawn_helper(
    args: dict,
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
    helper_workspace: str,
) -> str:
    """Compatibility endpoint: helper-side spawning is disabled."""
    _sync_delegate_action_globals()
    return json.dumps({
        "ok": False,
        "error": "helper_spawn_disabled",
        "blocked_reason": (
            "Helper creation, resource fulfillment, and resume activation are coordinated by the main process "
            "through delegate. This helper cannot spawn, wait for, resume, or kill other helpers.\n"
            "helper 的创建、资源补齐和恢复由主进程统一调度。"
        ),
        "dispatch_boundary_facts": (
            "Only the main process can create helpers, fulfill resources, activate resumes, wait for helpers, or kill helpers. "
            "Same-batch resource results, preserved helper workspaces, and the blocked helper report are the relevant recovery facts."
        ),
        "recovery_facts": {
            "coordinator": "main_process",
            "available_shapes": ["reuse_same_batch_resource", "spawn_resource_helper", "resume_same_task_id", "refuse_resource", "terminate_frozen_helper"],
        },
        "main_thread_action": (
            "The main process decides from this helper's report whether same-batch resources, a new resource helper, "
            "same-task resume, resource refusal, or termination fits the active task.\n"
            "主进程根据报告决定复用资源、派资源 helper、续作、拒绝或终止。"
        ),
        "suggested_tool": "request_resource",
    }, ensure_ascii=False)


async def handle_wait_helper(args: dict) -> str:
    """Compatibility endpoint: helper-side waiting is disabled."""
    _sync_delegate_action_globals()
    return json.dumps({
        "ok": False,
        "error": "helper_wait_disabled",
        "blocked_reason": (
            "A helper cannot wait for or activate other helpers; waiting, resource confirmation, and resume "
            "decisions are coordinated by the main process.\n"
            "helper 不能等待或激活其它 helper，相关调度由主进程负责。"
        ),
        "dispatch_boundary_facts": (
            "Only the main process can inspect global helper state, confirm resources, wait for helpers, activate resumes, or terminate helpers."
        ),
        "recovery_facts": {
            "coordinator": "main_process",
            "available_shapes": ["inspect_helper_state", "resume_same_task_id", "terminate_helper", "reuse_existing_resource", "spawn_resource_helper"],
        },
        "main_thread_action": (
            "The main process can inspect helper state with delegate/process tools, then decide whether resume, termination, "
            "existing resources, or a resource helper fits the active task.\n"
            "主进程检查状态后决定续作、终止、复用资源或派资源 helper。"
        ),
    }, ensure_ascii=False)

