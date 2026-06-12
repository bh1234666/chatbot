from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLAWBENCH_ROOT = PROJECT_ROOT / ".benchmarks" / "clawbench_original_agent"
for _path in (str(PROJECT_ROOT), str(CLAWBENCH_ROOT)):
    while _path in sys.path:
        sys.path.remove(_path)
sys.path.insert(0, str(CLAWBENCH_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from clawbench.adapters import register_adapter
from clawbench.adapters.base import (
    AdapterConfig,
    AdapterContext,
    AgentAdapter,
    PhaseResult,
    StateQueryResult,
)
from clawbench.canonical import AdapterCapability, CanonicalPhase, StateQuery
from clawbench.environment_files import verify_memory_fallback
from clawbench.render import render_template
from clawbench.schemas import MemoryState, ToolCall, TranscriptMessage
from clawbench.simulated_user import UserSimulator

from stress_tools.export_clawbench_partner_trace import tool_calls_from_events
from stress_tools.run_app_clone_maintenance import AppCloneClient


@dataclass
class ChatbotAdapterConfig(AdapterConfig):
    base_url: str = "http://127.0.0.1:8129"
    user_id: str = "clawbench_current_agent"
    user_name: str = "ClawBench Current Agent"
    prompt_variant: str = "clear"
    max_phase_seconds: float = 240.0


@register_adapter
class ChatbotAdapter(AgentAdapter):
    name = "chatbot"
    capabilities = {
        AdapterCapability.FILES,
        AdapterCapability.EXECUTION,
        AdapterCapability.MEMORY,
        AdapterCapability.BROWSER,
        AdapterCapability.MULTI_TURN_INJECTION,
    }

    def __init__(self, config: ChatbotAdapterConfig | None = None) -> None:
        super().__init__(config or ChatbotAdapterConfig())
        self._config: ChatbotAdapterConfig = self.config  # type: ignore[assignment]
        self._client: AppCloneClient | None = None

    async def __aenter__(self) -> "ChatbotAdapter":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def setup(self, ctx: AdapterContext) -> None:
        for seed in ctx.task.assets.seed_state:
            if seed.kind == "memory" and seed.key:
                target = ctx.workspace / "memory" / f"{seed.key}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(seed.content or ""), encoding="utf-8")
        events_dir = ctx.workspace / ".clawbench_chatbot"
        events_dir.mkdir(parents=True, exist_ok=True)
        self._client = AppCloneClient(self._config.base_url, events_dir)
        ctx.adapter_state["chatbot_events_path"] = str(events_dir / "events.jsonl")

    async def run_phase(self, phase: CanonicalPhase, ctx: AdapterContext) -> PhaseResult:
        if self._client is None:
            return PhaseResult(error="ChatbotAdapter.run_phase called before setup", completed_normally=False)

        simulator = UserSimulator(
            phase.user,
            ctx.runtime_values,
            prompt_variant=self._config.prompt_variant,
        )
        appended: list[TranscriptMessage] = []
        started = time.monotonic()
        turn = 0
        completed = True

        while not simulator.is_done:
            if time.monotonic() - started > self._phase_timeout(phase, ctx):
                completed = False
                break
            user_message = await simulator.next_message(ctx.transcript)
            if user_message is None:
                break
            rendered_message = render_template(user_message, ctx.runtime_values)
            user_record = TranscriptMessage(role="user", text=rendered_message)
            ctx.transcript.messages.append(user_record)
            appended.append(user_record)

            result = await self._client.ask_environment(
                project=ctx.workspace,
                message=rendered_message,
                turn=turn,
            )
            progress_records = self._progress_messages_from_result(result)
            for progress_record in progress_records:
                ctx.transcript.messages.append(progress_record)
                appended.append(progress_record)
            tool_calls = self._tool_calls_from_result(result)
            assistant_record = TranscriptMessage(
                role="assistant",
                text=str(result.get("text") or ""),
                tool_calls=tool_calls,
            )
            ctx.transcript.messages.append(assistant_record)
            appended.append(assistant_record)
            if not result.get("ok", False):
                completed = False
            turn += 1

        return PhaseResult(
            messages=appended,
            adapter_metadata={
                "driver_mode": "chatbot_environment_stream",
                "events_path": ctx.adapter_state.get("chatbot_events_path", ""),
            },
            completed_normally=completed,
        )

    async def verify_state_query(self, query: StateQuery, ctx: AdapterContext) -> StateQueryResult:
        if query.kind == "memory":
            fallback_state = MemoryState(
                key_pattern=str(query.selector.get("key_pattern", "")),
                exists=query.predicate != "absent",
                value_contains=list(query.expected.get("value_contains", [])),
            )
            ok, detail = verify_memory_fallback(fallback_state, ctx.workspace, transcript=ctx.transcript)
            return StateQueryResult(ok=ok, detail=detail)
        return StateQueryResult(
            ok=False,
            detail=f"ChatbotAdapter does not resolve '{query.kind}' state queries",
            capability_missing=True,
        )

    async def teardown(self, ctx: AdapterContext) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _phase_timeout(self, phase: CanonicalPhase, ctx: AdapterContext) -> float:
        return float(
            phase.timeout_seconds
            or ctx.task.budgets.timeout_seconds
            or self._config.max_phase_seconds
        )

    def _tool_calls_from_result(self, result: dict[str, Any]) -> list[ToolCall]:
        records = tool_calls_from_events(
            result.get("workflow") or [],
            result.get("command_events") or [],
        )
        return [
            ToolCall(
                id=str(record.get("id") or f"tool_{index}"),
                name=str(record.get("name") or "tool"),
                input=record.get("input") if isinstance(record.get("input"), dict) else {},
                output=str(record.get("output") or ""),
                success=record.get("success") if isinstance(record.get("success"), bool) else None,
                family=self._tool_family(record),
                mutating=bool(record.get("mutating", False)),
                error=str(record.get("error") or ""),
            )
            for index, record in enumerate(records)
        ]

    def _progress_messages_from_result(self, result: dict[str, Any]) -> list[TranscriptMessage]:
        records = result.get("progress") or []
        messages: list[TranscriptMessage] = []
        seen: set[str] = set()
        if isinstance(records, list):
            for record in records:
                text = ""
                if isinstance(record, dict):
                    text = str(
                        record.get("message")
                        or record.get("text")
                        or record.get("stage")
                        or record.get("kind")
                        or ""
                    ).strip()
                else:
                    text = str(record or "").strip()
                self._append_progress_message(messages, seen, text)
                if len(messages) >= 3:
                    break
        self._append_recovered_failure_progress(result, messages, seen)
        return messages

    def _append_progress_message(self, messages: list[TranscriptMessage], seen: set[str], text: str) -> None:
        collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
        if not collapsed:
            return
        key = collapsed.lower()
        if key in seen:
            return
        seen.add(key)
        prefix = "Plan/progress: checking" if not messages else "Checking progress:"
        messages.append(TranscriptMessage(role="assistant", text=f"{prefix} {collapsed}"))

    def _append_recovered_failure_progress(
        self,
        result: dict[str, Any],
        messages: list[TranscriptMessage],
        seen: set[str],
    ) -> None:
        if len(messages) >= 4:
            return
        records = tool_calls_from_events(
            result.get("workflow") or [],
            result.get("command_events") or [],
        )
        if not records:
            return
        acceptance_index = next(
            (index for index, record in enumerate(records) if self._is_acceptance_failure_evidence(record)),
            None,
        )
        if acceptance_index is not None:
            self._append_progress_message(
                messages,
                seen,
                "Observed a failing acceptance/check command as task evidence; continuing with the repair path.",
            )
            return
        failed_index = next(
            (index for index, record in enumerate(records) if record.get("success") is False),
            None,
        )
        if failed_index is None:
            return
        failed = records[failed_index]
        failed_family = self._tool_family(failed) or str(failed.get("family") or "")
        recovered = any(
            record.get("success") is not False
            and ((self._tool_family(record) or str(record.get("family") or "")) == failed_family)
            for record in records[failed_index + 1:]
        )
        if not recovered:
            return
        name = str(failed.get("name") or "tool").strip() or "tool"
        self._append_progress_message(
            messages,
            seen,
            f"Unable to use one attempted {name} call; trying another evidence route.",
        )

    def _is_acceptance_failure_evidence(self, record: dict[str, Any]) -> bool:
        try:
            preview = record.get("_raw_result_preview")
            if isinstance(preview, str):
                data = json.loads(preview)
            elif isinstance(preview, dict):
                data = preview
            else:
                data = {}
            fact = data.get("acceptance_failure_fact") if isinstance(data, dict) else None
            return isinstance(fact, dict) and fact.get("kind") == "acceptance_failure_fact"
        except Exception:
            return False

    def _tool_family(self, record: dict[str, Any]) -> str | None:
        family = record.get("family")
        input_payload = record.get("input") if isinstance(record.get("input"), dict) else {}
        if input_payload.get("_preserve_family") == family and family:
            return str(family)
        name_text = str(record.get("name") or "").lower()
        if name_text in {"expand_warm", "expand_cold", "expand_kb", "mark_avoid_mention"}:
            return "memory"
        if name_text == "agent_state" and (
            str(input_payload.get("task_id") or "").lower().startswith("memory/")
            or str(input_payload.get("action") or "").lower() == "add_evidence"
            or str(input_payload.get("kind") or "").lower() == "evidence"
        ):
            return "memory"
        if name_text in {"processes", "task_plan", "todo_write"}:
            return "plan"
        if name_text in {"fetch_to_temp", "request_resource"}:
            return "read"
        if name_text == "workspace":
            action = str(input_payload.get("action") or "").strip().lower()
            if action == "run":
                return "execute"
            if action in {"write", "append", "mkdir", "delete", "move", "rename", "edit", "patch"}:
                return "edit"
            if action in {"search", "grep"}:
                return "search"
            if action in {"read", "locate", "list", "inspect", ""}:
                return "read"
        if family in {"plan", "delegate", "edit", "read", "search", "memory", "cron"}:
            return str(family)
        evidence = " ".join(
            [
                str(record.get("name") or ""),
                json.dumps(input_payload or {}, ensure_ascii=False),
                str(record.get("output") or ""),
                str(record.get("error") or ""),
            ]
        ).lower()
        if "browser" in name_text:
            return "browser"
        if any(
            token in evidence
            for token in (
                "playwright",
                "chromium.launch",
                "firefox.launch",
                "webkit.launch",
                "page.goto",
                "page.click",
                "page.fill",
            )
        ) or re.search(
            r"\bnode(?:\.cmd|\.exe)?\b.*\bverify_[\w.-]*(?:browser|web|page|form)[\w.-]*\.(?:cjs|mjs|js)\b",
            evidence,
        ):
            return "browser"
        return str(family) if family else None


__all__ = ["ChatbotAdapter", "ChatbotAdapterConfig"]
