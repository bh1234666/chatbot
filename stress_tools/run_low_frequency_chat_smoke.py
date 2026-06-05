from __future__ import annotations

import argparse
import asyncio
import codecs
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "stress_tools" / "runs" / "low_frequency_chat_smoke"


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


async def iter_sse_lines(response: httpx.Response):
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    async for chunk in response.aiter_bytes():
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            yield line.rstrip("\r")
    tail = pending + decoder.decode(b"", final=True)
    if tail:
        yield tail.rstrip("\r")


async def create_archive_and_group(client: httpx.AsyncClient, base_url: str) -> tuple[str, str]:
    archive_name = "low_freq_chat_smoke_" + now_id()
    r = await client.post(f"{base_url}/v1/archives", json={"name": archive_name, "persona_id": "猫娘"})
    r.raise_for_status()
    archive_id = r.json()["archive_id"]
    group_id = "low_freq_smoke_" + uuid.uuid4().hex[:8]
    r = await client.post(
        f"{base_url}/v1/bot/groups/{group_id}/join",
        json={"archive_id": archive_id, "group_name": "low frequency smoke", "persona_label": "猫娘"},
    )
    r.raise_for_status()
    return archive_id, group_id


async def chat_once(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    archive_id: str,
    group_id: str,
    user_id: str,
    user_name: str,
    message: str,
    turn: int,
) -> dict[str, Any]:
    payload = {
        "archive_id": archive_id,
        "group_id": group_id,
        "user_id": user_id,
        "user_name": user_name,
        "message": message,
        "client_msg_id": f"lf_{turn}_{uuid.uuid4().hex[:8]}",
    }
    started = time.monotonic()
    event = "message"
    data_lines: list[str] = []
    tokens: list[str] = []
    events: dict[str, list[Any]] = {"progress": [], "workflow": [], "error": [], "done": [], "meta": []}
    async with client.stream("POST", f"{base_url}/v1/chat/stream", json=payload) as r:
        if r.status_code >= 400:
            body = await r.aread()
            return {
                "ok": False,
                "status_code": r.status_code,
                "latency_sec": round(time.monotonic() - started, 3),
                "error": body.decode("utf-8", errors="replace")[:2000],
            }
        async for line in iter_sse_lines(r):
            if line == "":
                if data_lines:
                    raw = "\n".join(data_lines)
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {"raw": raw}
                    if event == "token":
                        tokens.append(str(data.get("text") or ""))
                    elif event in events:
                        events[event].append(data)
                    elif event == "complete":
                        break
                event = "message"
                data_lines = []
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
    text = "".join(tokens).strip()
    lower = text.lower()
    issues: list[str] = []
    if not text:
        issues.append("empty_reply")
    for marker in ("_env", "_delegate", "trace_id", "system prompt", "round2", "workspace."):
        if marker in lower:
            issues.append(f"internal_leak:{marker}")
    if "猫" not in text and "喵" not in text and "你" not in text:
        issues.append("persona_or_reply_style_unclear")
    if events["error"]:
        issues.append("sse_error_event")
    return {
        "ok": not issues,
        "status_code": 200,
        "latency_sec": round(time.monotonic() - started, 3),
        "text": text,
        "issues": issues,
        "event_counts": {k: len(v) for k, v in events.items()},
        "errors": events["error"],
    }


async def run(args: argparse.Namespace) -> Path:
    run_dir = RUNS_DIR / ("lf_" + now_id())
    run_dir.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout_sec, connect=10.0), trust_env=False) as client:
        health = await client.get(f"{base_url}/health")
        health.raise_for_status()
        archive_id, group_id = await create_archive_and_group(client, base_url)
        messages = [
            ("u_alice", "Alice", "大家晚上好，简单聊两句，别暴露内部环境。"),
            ("u_bob", "Bob", "你现在是什么人设？用一句话回答就行。"),
        ]
        results = []
        for turn, (user_id, user_name, message) in enumerate(messages):
            results.append(
                await chat_once(
                    client,
                    base_url,
                    archive_id=archive_id,
                    group_id=group_id,
                    user_id=user_id,
                    user_name=user_name,
                    message=message,
                    turn=turn,
                )
            )
            if turn + 1 < len(messages):
                await asyncio.sleep(args.gap_sec)
    summary = {
        "archive_id": archive_id,
        "group_id": group_id,
        "results": results,
        "ok": all(r.get("ok") for r in results),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Low Frequency Chat Smoke", "", f"- ok: {summary['ok']}", f"- archive_id: {archive_id}", f"- group_id: {group_id}", ""]
    for idx, result in enumerate(results):
        lines.append(f"## Turn {idx}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(result, ensure_ascii=False, indent=2))
        lines.append("```")
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--gap-sec", type=float, default=60.0)
    parser.add_argument("--timeout-sec", type=float, default=600.0)
    return parser.parse_args()


if __name__ == "__main__":
    print(asyncio.run(run(parse_args())))
