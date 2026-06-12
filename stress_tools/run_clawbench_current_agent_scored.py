from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLAWBENCH_ROOT = PROJECT_ROOT / ".benchmarks" / "clawbench_original_agent"
for _path in (str(PROJECT_ROOT), str(CLAWBENCH_ROOT)):
    while _path in sys.path:
        sys.path.remove(_path)
sys.path.insert(0, str(CLAWBENCH_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from clawbench.adapters.base import AdapterContext
from clawbench.canonical import AdapterCapability
from clawbench.canonical.convert import from_task_definition
from clawbench.client import GatewayConfig
from clawbench.harness import BenchmarkHarness
from clawbench.environment_files import MEMORY_FILE_CANDIDATES
from clawbench.schemas import TokenUsage, Transcript
from clawbench.scorer import score_task_run
import clawbench.services as _clawbench_services
from clawbench.services import build_runtime_values, start_background_services, stop_background_services
from clawbench.tasks import load_all_tasks

from stress_tools.clawbench_chatbot_agent_adapter import ChatbotAdapter, ChatbotAdapterConfig
from stress_tools.run_app_clone_maintenance import start_service, stop_service, wait_health


_ORIGINAL_STOP_BACKGROUND_SERVICES = stop_background_services


async def _stop_background_services_compat(services: list[Any]) -> None:
    if os.name != "nt":
        await _ORIGINAL_STOP_BACKGROUND_SERVICES(services)
        return
    for service in reversed(services):
        process = getattr(service, "process", None)
        if process is None or process.poll() is not None:
            continue
        try:
            process.terminate()
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=3)
            continue
        except Exception:
            pass
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=3)
        except Exception:
            pass


stop_background_services = _stop_background_services_compat
_clawbench_services.stop_background_services = _stop_background_services_compat


def _source_fingerprint(repo_root: Path) -> str:
    import hashlib

    rows: list[str] = []
    for target_name in ("app", "stress_tools"):
        target = repo_root / target_name
        if not target.exists():
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or "runs" in path.parts:
                continue
            if path.suffix in {".pyc", ".log"}:
                continue
            rel = path.relative_to(repo_root).as_posix()
            stat = path.stat()
            rows.append(f"{rel}|{stat.st_size}|{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _adapter_capabilities(config: ChatbotAdapterConfig) -> set[AdapterCapability]:
    return ChatbotAdapter.supported_capabilities(config)


def _missing_capabilities_for_task(task: Any, adapter_caps: set[AdapterCapability]) -> list[str]:
    canonical = from_task_definition(task)
    missing = set(canonical.required_adapter_capabilities) - adapter_caps
    return sorted(cap.value for cap in missing)


def _split_tasks_by_capability(
    tasks: list[Any],
    *,
    adapter_caps: set[AdapterCapability],
    enabled: bool,
) -> tuple[list[Any], list[dict[str, Any]]]:
    runnable: list[Any] = []
    skipped: list[dict[str, Any]] = []
    for task in tasks:
        missing = _missing_capabilities_for_task(task, adapter_caps) if enabled else []
        if missing:
            skipped.append(
                {
                    "task_id": task.id,
                    "missing_capabilities": missing,
                    "required_capabilities": sorted(
                        cap.value for cap in from_task_definition(task).required_adapter_capabilities
                    ),
                    "reason": "adapter_capability_gap",
                }
            )
        else:
            runnable.append(task)
    return runnable, skipped


def _requested_task_ids(args: argparse.Namespace) -> list[str] | None:
    if bool(getattr(args, "all_public", False)):
        return None
    return list(args.task or ["t1-fs-quick-note"])


def _cache_stats_from_debug_logs(run_root: Path) -> list[Any]:
    debug_dir = run_root / "debug_logs"
    if not debug_dir.is_dir():
        return []
    try:
        from app.core.cache_report import parse_debug_log_text
    except Exception:
        return []
    stats: list[Any] = []
    for path in sorted(debug_dir.glob("*.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            stats.extend(
                item
                for item in parse_debug_log_text(text).stats
                if not str(getattr(item, "tag", "") or "").startswith("helper_kind.")
            )
        except Exception:
            continue
    return stats


def _token_usage_from_cache_stats(stats: list[Any]) -> TokenUsage:
    input_tokens = sum(int(getattr(item, "prompt_tokens", 0) or 0) for item in stats)
    output_tokens = sum(int(getattr(item, "completion_tokens", 0) or 0) for item in stats)
    cache_read_tokens = sum(int(getattr(item, "cache_hit_tokens", 0) or 0) for item in stats)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=0,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=0,
        total_tokens=input_tokens + output_tokens,
        total_cost_usd=0.0,
    )


class _TranscriptMemorySearchClient:
    """Small memory.search shim for direct adapters without a gateway RPC.

    It exposes the same read-only facts ClawBench's fallback verifier accepts:
    well-known workspace memory files plus memory-like transcript writes.
    """

    def __init__(self, transcript: Transcript, workspace: Path | None = None) -> None:
        self._transcript = transcript
        self._workspace = workspace

    async def _rpc(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method != "memory.search":
            raise RuntimeError(f"unsupported direct-adapter RPC: {method}")
        query = str(payload.get("query") or "")
        limit = int(payload.get("limit") or 20)
        entries: list[dict[str, str]] = []
        for key, value in self._workspace_memory_entries():
            if _memory_query_matches(query, f"{key}\n{value}"):
                entries.append({"key": key, "value": value})
                if len(entries) >= limit:
                    return {"payload": {"entries": entries}}
        for call in self._transcript.tool_call_sequence:
            family = str(call.family or "").lower()
            name = str(call.name or "").lower()
            input_payload = call.input if isinstance(call.input, dict) else {}
            task_id = str(input_payload.get("task_id") or "")
            action = str(input_payload.get("action") or "").lower()
            kind = str(input_payload.get("kind") or "").lower()
            if name in {"expand_warm", "expand_cold", "expand_kb"}:
                continue
            if (
                family != "memory"
                and not task_id.lower().startswith("memory/")
                and not (
                    name == "agent_state"
                    and (action in {"add_evidence", "upsert_contract"} or kind == "evidence")
                )
            ):
                continue
            if family == "memory" and "search" in name and "write" not in name and "store" not in name:
                continue
            parts = [
                task_id,
                str(input_payload.get("goal") or ""),
                str(input_payload.get("summary") or ""),
                str(input_payload.get("current_stage") or ""),
                str(input_payload.get("status") or ""),
            ]
            acceptance = input_payload.get("acceptance")
            if isinstance(acceptance, list):
                parts.extend(str(item) for item in acceptance)
            elif acceptance:
                parts.append(str(acceptance))
            try:
                parts.append(json.dumps(input_payload, ensure_ascii=False, sort_keys=True))
            except TypeError:
                parts.append(str(input_payload))
            parts.extend([str(call.output or ""), str(call.error or "")])
            value = "\n".join(part for part in parts if part)
            if _memory_query_matches(query, value):
                entries.append({"key": task_id, "value": value})
                if len(entries) >= limit:
                    break
        return {"payload": {"entries": entries}}

    async def get_agent_file(self, _agent_id: str, _file_name: str) -> dict[str, Any]:
        if self._workspace is None:
            return {"file": {"content": ""}}
        try:
            path = (self._workspace / _file_name).resolve()
            path.relative_to(self._workspace.resolve())
            if path.is_file():
                return {"file": {"content": path.read_text(encoding="utf-8", errors="replace")}}
        except Exception:
            pass
        return {"file": {"content": ""}}

    def _workspace_memory_entries(self) -> list[tuple[str, str]]:
        if self._workspace is None:
            return []
        entries: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        workspace_root = self._workspace.resolve()
        for name in MEMORY_FILE_CANDIDATES:
            try:
                path = (self._workspace / name).resolve()
                path.relative_to(workspace_root)
                if not path.is_file():
                    continue
                path_key = str(path).casefold()
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if text.strip():
                entries.append((name, text))
        return entries


def _memory_query_matches(query: str, value: str) -> bool:
    if not query:
        return True
    try:
        return re.search(query, value, re.IGNORECASE) is not None
    except re.error:
        return query.lower() in value.lower()


def _usage_metadata_from_cache_stats(stats: list[Any]) -> dict[str, Any]:
    prompt_tokens = sum(int(getattr(item, "prompt_tokens", 0) or 0) for item in stats)
    completion_tokens = sum(int(getattr(item, "completion_tokens", 0) or 0) for item in stats)
    cache_hit_tokens = sum(int(getattr(item, "cache_hit_tokens", 0) or 0) for item in stats)
    cache_miss_tokens = sum(int(getattr(item, "cache_miss_tokens", 0) or 0) for item in stats)
    total_prompt_cache = cache_hit_tokens + cache_miss_tokens
    by_model: dict[str, dict[str, int]] = {}
    by_tag: dict[str, dict[str, int]] = {}
    for item in stats:
        model = str(getattr(item, "model", "") or "unknown")
        tag = str(getattr(item, "tag", "") or "unknown")
        for bucket, key in ((by_model, model), (by_tag, tag)):
            row = bucket.setdefault(
                key,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 0,
                },
            )
            row["calls"] += 1
            row["prompt_tokens"] += int(getattr(item, "prompt_tokens", 0) or 0)
            row["completion_tokens"] += int(getattr(item, "completion_tokens", 0) or 0)
            row["cache_hit_tokens"] += int(getattr(item, "cache_hit_tokens", 0) or 0)
            row["cache_miss_tokens"] += int(getattr(item, "cache_miss_tokens", 0) or 0)
    return {
        "source": "debug_logs.llm.cache_stats",
        "calls": len(stats),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cache_hit_rate": (cache_hit_tokens / total_prompt_cache if total_prompt_cache else None),
        "cost_estimated": False,
        "cost_note": "No authoritative model price table is configured for this runner; token counts are recorded without estimated USD cost.",
        "excluded_alias_tags": ["helper_kind.*"],
        "by_model": by_model,
        "by_tag": by_tag,
    }


def _attach_usage_to_transcript(transcript: Transcript, stats: list[Any]) -> dict[str, Any]:
    metadata = _usage_metadata_from_cache_stats(stats)
    if not stats:
        metadata["source"] = "debug_logs.llm.cache_stats_missing"
        return metadata
    usage = _token_usage_from_cache_stats(stats)
    target = next((message for message in reversed(transcript.messages) if message.role == "assistant"), None)
    if target is None:
        metadata["source"] = "debug_logs.llm.cache_stats_unattached"
        return metadata
    target.usage = target.usage.merged(usage)
    return metadata


def _write_python3_shim(workspace: Path) -> None:
    shim_py = workspace / ".clawbench_python3_shim.py"
    shim_py.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import runpy",
                "import sys",
                "",
                "try:",
                "    sys.stdout.reconfigure(newline='\\n')",
                "    sys.stderr.reconfigure(newline='\\n')",
                "except Exception:",
                "    pass",
                "",
                "args = sys.argv[1:]",
                "if not args:",
                "    raise SystemExit('python3 shim requires a script, -m module, or -c code')",
                "if args[0] == '-m':",
                "    if len(args) < 2:",
                "        raise SystemExit('python3 -m requires a module name')",
                "    module = args[1]",
                "    sys.argv = [module, *args[2:]]",
                "    runpy.run_module(module, run_name='__main__', alter_sys=True)",
                "elif args[0] == '-c':",
                "    if len(args) < 2:",
                "        raise SystemExit('python3 -c requires code')",
                "    sys.argv = ['-c', *args[2:]]",
                "    exec(compile(args[1], '<python3-shim -c>', 'exec'), {'__name__': '__main__'})",
                "else:",
                "    sys.argv = [args[0], *args[1:]]",
                "    runpy.run_path(args[0], run_name='__main__')",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    python3_cmd = workspace / "python3.cmd"
    python3_cmd.write_text(f'@"{sys.executable}" "{shim_py}" %*\r\n', encoding="utf-8")
    pytest_cmd = workspace / "pytest.cmd"
    pytest_cmd.write_text(f'@"{sys.executable}" -m pytest %*\r\n', encoding="utf-8")


def _bundled_node_paths() -> tuple[Path | None, Path | None]:
    """Return the desktop runtime Node executable and package root when present."""
    candidates = []
    env_node = os.environ.get("CLAWBENCH_NODE_EXE") or os.environ.get("CODEX_NODE_EXE")
    if env_node:
        candidates.append(Path(env_node))
    home = Path.home()
    candidates.append(
        home
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
        / "bin"
        / ("node.exe" if os.name == "nt" else "node")
    )
    candidates.append(Path(sys.executable).resolve().parent / ("node.exe" if os.name == "nt" else "node"))

    node_exe = next((path for path in candidates if path.is_file()), None)

    module_candidates = []
    env_modules = os.environ.get("CLAWBENCH_NODE_PATH") or os.environ.get("NODE_PATH")
    if env_modules:
        module_candidates.extend(Path(part) for part in env_modules.split(os.pathsep) if part)
    module_candidates.append(
        home
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
        / "node_modules"
    )
    module_candidates.append(CLAWBENCH_ROOT / "node_modules")
    node_modules = next((path for path in module_candidates if path.is_dir()), None)
    return node_exe, node_modules


def _system_chromium_path() -> Path | None:
    candidates: list[Path] = []
    env_value = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env_value:
        candidates.append(Path(env_value))
    if os.name == "nt":
        candidates.extend(
            [
                Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
                Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
                Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
            ]
        )
    else:
        candidates.extend(Path(name) for name in ("google-chrome", "chromium", "chromium-browser"))
    return next((path for path in candidates if path.is_file()), None)


def _write_playwright_chromium_patch(workspace: Path, executable_path: Path) -> Path:
    patch_path = workspace / ".clawbench_playwright_chromium_patch.cjs"
    escaped = str(executable_path).replace("\\", "\\\\")
    patch_path.write_text(
        "\n".join(
            [
                "const Module = require('module');",
                f"const fallbackExecutable = '{escaped}';",
                "const originalLoad = Module._load;",
                "function patchPlaywright(exports) {",
                "  if (!exports || !exports.chromium || exports.chromium.__clawbenchPatched) return exports;",
                "  const originalLaunch = exports.chromium.launch.bind(exports.chromium);",
                "  exports.chromium.launch = (options = {}) => {",
                "    if (!options.executablePath && !options.channel) {",
                "      options = { ...options, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || fallbackExecutable };",
                "    }",
                "    return originalLaunch(options);",
                "  };",
                "  Object.defineProperty(exports.chromium, '__clawbenchPatched', { value: true });",
                "  return exports;",
                "}",
                "Module._load = function patchedLoad(request, parent, isMain) {",
                "  const exports = originalLoad.apply(this, arguments);",
                "  return request === 'playwright' ? patchPlaywright(exports) : exports;",
                "};",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    return patch_path


def _prepare_node_runtime(workspace: Path) -> None:
    node_exe, node_modules = _bundled_node_paths()
    if node_exe is None:
        return
    node_path_roots: list[str] = []
    if node_modules is not None:
        node_path_roots.append(str(node_modules))
        pnpm_public_root = node_modules / ".pnpm" / "node_modules"
        if pnpm_public_root.is_dir():
            node_path_roots.append(str(pnpm_public_root))
        existing_node_path = os.environ.get("NODE_PATH", "")
        node_path_parts = list(node_path_roots)
        if existing_node_path:
            node_path_parts.append(existing_node_path)
        os.environ["NODE_PATH"] = os.pathsep.join(dict.fromkeys(node_path_parts))
    chrome_path = _system_chromium_path()
    playwright_patch = (
        _write_playwright_chromium_patch(workspace, chrome_path)
        if chrome_path is not None
        else None
    )
    existing_path = os.environ.get("PATH", "")
    path_parts = [str(workspace), str(node_exe.parent)]
    if chrome_path is not None:
        path_parts.append(str(chrome_path.parent))
    if existing_path:
        path_parts.append(existing_path)
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(path_parts))
    if os.name == "nt":
        lines = []
        if node_path_roots:
            lines.append(f'@set "NODE_PATH={";".join(node_path_roots)};%NODE_PATH%"')
        if playwright_patch is not None:
            lines.append(f'@set "NODE_OPTIONS=--require {playwright_patch} %NODE_OPTIONS%"')
        lines.append(f'@"{node_exe}" %*')
        (workspace / "node.cmd").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    else:
        node_shim = workspace / "node"
        node_path_line = (
            f'export NODE_PATH="{":".join(node_path_roots)}${{NODE_PATH:+:$NODE_PATH}}"\n'
            if node_path_roots
            else ""
        )
        node_options_line = (
            f'export NODE_OPTIONS="--require {playwright_patch}${{NODE_OPTIONS:+ $NODE_OPTIONS}}"\n'
            if playwright_patch is not None
            else ""
        )
        node_shim.write_text(
            f'#!/usr/bin/env sh\n{node_path_line}{node_options_line}exec "{node_exe}" "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        node_shim.chmod(0o755)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    run_stamp = time.strftime("%Y%m%d_%H%M%S")
    run_root_base = PROJECT_ROOT / "stress_tools" / "runs" / "clawbench_current_scored"
    run_root_base.mkdir(parents=True, exist_ok=True)
    run_root: Path | None = None
    for counter in range(100):
        suffix = "" if counter == 0 else f"_p{os.getpid()}" + (f"_{counter}" if counter > 1 else "")
        candidate = run_root_base / f"{run_stamp}{suffix}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            run_root = candidate
            break
        except FileExistsError:
            continue
    if run_root is None:
        import uuid
        run_root = run_root_base / f"{run_stamp}_p{os.getpid()}_{uuid.uuid4().hex[:8]}"
        run_root.mkdir(parents=False, exist_ok=False)
    os.environ["OPENCLAW_STATE_DIR"] = str(run_root / "state")

    base_url = f"http://127.0.0.1:{args.port}"
    task_ids = _requested_task_ids(args)
    tasks = load_all_tasks(
        tasks_dir=CLAWBENCH_ROOT / "tasks-public",
        task_ids=task_ids,
        prompt_variant=args.prompt_variant,
        pool=args.pool,
    )
    if not tasks:
        raise RuntimeError(f"No ClawBench tasks matched: {task_ids}")
    adapter_config = ChatbotAdapterConfig(
        model=args.model,
        base_url=base_url,
        prompt_variant=args.prompt_variant,
        max_phase_seconds=args.max_phase_seconds,
    )
    adapter_caps = _adapter_capabilities(adapter_config)
    runnable_tasks, skipped_tasks = _split_tasks_by_capability(
        tasks,
        adapter_caps=adapter_caps,
        enabled=args.capability_gating,
    )
    result_json = args.output or (run_root / "current_agent_clawbench_scored.json")
    result_json.parent.mkdir(parents=True, exist_ok=True)
    if not runnable_tasks:
        payload = {
            "model": args.model,
            "provider": "chatbot",
            "environment": {
                "task_count": 0,
                "requested_task_ids": task_ids,
                "runnable_task_ids": [],
                "skipped_tasks": skipped_tasks,
                "capability_gating": bool(args.capability_gating),
                "adapter": "chatbot",
                "adapter_capabilities": sorted(cap.value for cap in adapter_caps),
                "current_agent_source_fingerprint": _source_fingerprint(PROJECT_ROOT),
                "run_root": str(run_root),
                "interface": "stress_tools/run_clawbench_current_agent_scored.py",
            },
            "overall_score": 0.0,
            "overall_completion": 0.0,
            "overall_trajectory": 0.0,
            "overall_behavior": 0.0,
            "overall_reliability": 0.0,
            "task_results": [],
            "result_json": str(result_json),
            "skipped_tasks": skipped_tasks,
        }
        result_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_root / "summary.json").write_text(
            json.dumps(
                {
                    "result_json": str(result_json),
                    "overall_score": payload["overall_score"],
                    "overall_completion": payload["overall_completion"],
                    "overall_trajectory": payload["overall_trajectory"],
                    "overall_behavior": payload["overall_behavior"],
                    "overall_reliability": payload["overall_reliability"],
                    "task_results": [],
                    "skipped_tasks": skipped_tasks,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            from stress_tools.update_clawbench_progress_table import write_progress_table

            write_progress_table()
        except Exception as exc:
            print(f"[WARN] failed to update CLAWBENCH progress table: {exc}", file=sys.stderr)
        return payload | {"result_json": str(result_json)}

    proc = None
    if args.start_service:
        proc = start_service(run_root, args.port)
    try:
        await wait_health(base_url, args.health_timeout_sec)
        harness = BenchmarkHarness(
            gateway_config=GatewayConfig(url="ws://127.0.0.1:0", token="unused"),
            model=args.model,
            provider="chatbot",
            runs_per_task=args.runs,
            task_ids=[task.id for task in runnable_tasks],
            prompt_variant=args.prompt_variant,
            pool=args.pool,
            print_report=False,
            quiet=True,
            adapter="chatbot",
        )
        all_results = {task.id: [] for task in runnable_tasks}
        usage_stat_offset = 0
        run_usage_metadata: list[dict[str, Any]] = []
        per_run_dir = run_root / "per_run"
        per_run_dir.mkdir(parents=True, exist_ok=True)

        def write_scored_snapshot(*, partial: bool) -> dict[str, Any]:
            completed_tasks = [task for task in runnable_tasks if all_results.get(task.id)]
            aggregate_tasks = completed_tasks if partial else runnable_tasks
            aggregate_results = (
                {task.id: all_results.get(task.id, []) for task in aggregate_tasks}
                if partial
                else all_results
            )
            benchmark = harness._aggregate(aggregate_tasks, aggregate_results)
            snapshot = benchmark.model_dump(mode="json")
            snapshot["environment"]["adapter"] = "chatbot"
            snapshot["environment"]["requested_task_ids"] = task_ids
            snapshot["environment"]["runnable_task_ids"] = [task.id for task in runnable_tasks]
            snapshot["environment"]["completed_task_ids"] = [task.id for task in completed_tasks]
            snapshot["environment"]["partial"] = bool(partial)
            snapshot["environment"]["skipped_tasks"] = skipped_tasks
            snapshot["environment"]["capability_gating"] = bool(args.capability_gating)
            snapshot["environment"]["adapter_capabilities"] = sorted(cap.value for cap in adapter_caps)
            snapshot["environment"]["usage_source"] = "debug_logs.llm.cache_stats"
            snapshot["environment"]["cost_estimated"] = False
            snapshot["environment"]["current_agent_source_fingerprint"] = _source_fingerprint(PROJECT_ROOT)
            snapshot["environment"]["run_root"] = str(run_root)
            snapshot["environment"]["interface"] = "stress_tools/run_clawbench_current_agent_scored.py"
            snapshot["result_json"] = str(result_json)
            snapshot["skipped_tasks"] = skipped_tasks
            snapshot["run_usage_metadata"] = run_usage_metadata
            result_json.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            (run_root / "summary.json").write_text(
                json.dumps(
                    {
                        "result_json": str(result_json),
                        "partial": bool(partial),
                        "overall_score": snapshot.get("overall_score"),
                        "overall_completion": snapshot.get("overall_completion"),
                        "overall_trajectory": snapshot.get("overall_trajectory"),
                        "overall_behavior": snapshot.get("overall_behavior"),
                        "overall_reliability": snapshot.get("overall_reliability"),
                        "overall_input_tokens": snapshot.get("overall_input_tokens"),
                        "overall_output_tokens": snapshot.get("overall_output_tokens"),
                        "overall_total_tokens": snapshot.get("overall_total_tokens"),
                        "overall_cost_usd": snapshot.get("overall_cost_usd"),
                        "task_results": snapshot.get("task_results", []),
                        "skipped_tasks": skipped_tasks,
                        "run_usage_metadata": run_usage_metadata,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            try:
                from stress_tools.update_clawbench_progress_table import write_progress_table

                write_progress_table()
            except Exception as exc:
                print(f"[WARN] failed to update CLAWBENCH progress table: {exc}", file=sys.stderr)
            return snapshot

        async with ChatbotAdapter(adapter_config) as adapter:
            for task in runnable_tasks:
                for run_index in range(args.runs):
                    workspace = harness._create_run_workspace(task, run_index)
                    harness._setup_workspace(task, workspace)
                    if os.name == "nt":
                        _write_python3_shim(workspace)
                    _prepare_node_runtime(workspace)
                    runtime_values = build_runtime_values(
                        workspace=workspace,
                        repo_root=CLAWBENCH_ROOT,
                        extra={
                            "task_id": task.id,
                            "model": args.model,
                            "prompt_variant": args.prompt_variant,
                        },
                    )
                    services, runtime_values = await start_background_services(
                        task.setup.background_services,
                        workspace=workspace,
                        repo_root=CLAWBENCH_ROOT,
                        runtime_values=runtime_values,
                    )
                    transcript = Transcript()
                    ctx = AdapterContext(
                        task=from_task_definition(task),
                        workspace=workspace,
                        runtime_values=runtime_values,
                        run_index=run_index,
                        model=args.model,
                        transcript=transcript,
                    )
                    start_ms = int(time.monotonic() * 1000)
                    try:
                        await adapter.setup(ctx)
                        for phase in ctx.task.phases:
                            await adapter.run_phase(phase, ctx)
                        all_stats = _cache_stats_from_debug_logs(run_root)
                        new_stats = all_stats[usage_stat_offset:]
                        usage_stat_offset = len(all_stats)
                        usage_metadata = _attach_usage_to_transcript(transcript, new_stats)
                        usage_metadata["task_id"] = task.id
                        usage_metadata["run_index"] = run_index
                        result = await score_task_run(
                            task=task,
                            transcript=transcript,
                            workspace=workspace,
                            client=_TranscriptMemorySearchClient(transcript, workspace),  # type: ignore[arg-type]
                            session_key="",
                            agent_id="chatbot-direct-adapter",
                            duration_ms=int(time.monotonic() * 1000) - start_ms,
                            runtime_values=runtime_values,
                            judge_model=args.judge_model,
                            judge_affects_score=args.judge_affects_score,
                        )
                        result.run_index = run_index
                        all_results[task.id].append(result)
                        result_payload = result.model_dump(mode="json")
                        result_payload["adapter_usage_metadata"] = usage_metadata
                        run_usage_metadata.append(usage_metadata)
                        (per_run_dir / f"{task.id}_run{run_index}.json").write_text(
                            json.dumps(result_payload, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        write_scored_snapshot(partial=True)
                    finally:
                        await adapter.teardown(ctx)
                        await stop_background_services(services)

        payload = write_scored_snapshot(partial=False)
        return payload | {"result_json": str(result_json)}
    finally:
        if args.start_service:
            stop_service(proc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run current chatbot agent through ClawBench scoring.")
    parser.add_argument("--task", action="append", default=None)
    parser.add_argument("--all-public", action="store_true", help="Run every public task instead of the default smoke task.")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--model", default="chatbot-current-agent")
    parser.add_argument("--port", type=int, default=8129)
    parser.add_argument("--pool", default="public_dev")
    parser.add_argument("--prompt-variant", default="clear")
    parser.add_argument("--health-timeout-sec", type=float, default=120.0)
    parser.add_argument("--max-phase-seconds", type=float, default=240.0)
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--judge-affects-score", action="store_true")
    parser.add_argument("--capability-gating", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--start-service", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    result = asyncio.run(_run(parse_args()))
    print(
        json.dumps(
            {
                "result_json": result.get("result_json"),
                "overall_score": result.get("overall_score"),
                "overall_completion": result.get("overall_completion"),
                "overall_trajectory": result.get("overall_trajectory"),
                "overall_behavior": result.get("overall_behavior"),
                "overall_reliability": result.get("overall_reliability"),
                "overall_input_tokens": result.get("overall_input_tokens"),
                "overall_output_tokens": result.get("overall_output_tokens"),
                "overall_total_tokens": result.get("overall_total_tokens"),
                "overall_cost_usd": result.get("overall_cost_usd"),
                "task_results": result.get("task_results", []),
                "skipped_tasks": result.get("skipped_tasks", []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
