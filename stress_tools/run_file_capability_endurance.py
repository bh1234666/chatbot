from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from _longrun_test.scenario_runner import prepare_materials


RUNS_DIR = PROJECT_ROOT / "stress_tools" / "runs" / "file_endurance"


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Recorder:
    run_dir: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(self, event: dict[str, Any]) -> None:
        row = {"ts": now_iso(), **event}
        async with self.lock:
            with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def upload_file(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    archive_id: str,
    group_id: str,
    path: Path,
) -> str:
    r = await client.post(
        f"{base_url}/v1/chat/files/{archive_id}/{group_id}/upload",
        params={"filename": path.name, "user_id": "file_endurance", "user_name": "FileEndurance"},
        content=path.read_bytes(),
        headers={"content-type": "application/octet-stream"},
        timeout=300,
    )
    r.raise_for_status()
    data = r.json()
    item = data.get("file") or data
    return str(item.get("file_id") or item.get("id") or "")


async def stream_chat(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    archive_id: str,
    group_id: str,
    user_id: str,
    user_name: str,
    message: str,
    attached_file_ids: list[str] | None = None,
    timeout_sec: float = 1800.0,
) -> dict[str, Any]:
    payload = {
        "archive_id": archive_id,
        "group_id": group_id,
        "user_id": user_id,
        "user_name": user_name,
        "message": message,
        "client_msg_id": str(uuid.uuid4()),
        "current_dir": "",
        "project_id": "",
        "persona_id": "environment",
        "attached_file_ids": attached_file_ids or [],
    }
    started = time.monotonic()
    tokens: list[str] = []
    trace_id = ""
    events: dict[str, list[Any]] = {"progress": [], "done": [], "error": [], "workflow": [], "command": [], "meta": []}
    current_event = "message"
    async with client.stream("POST", f"{base_url}/v1/chat/stream", json=payload, timeout=timeout_sec) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", errors="replace")
            return {
                "ok": False,
                "status_code": resp.status_code,
                "latency_sec": round(time.monotonic() - started, 3),
                "error": body[:2000],
            }
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            raw = line.split(":", 1)[1].strip()
            try:
                data = json.loads(raw)
            except Exception:
                data = {"raw": raw}
            if current_event == "meta":
                trace_id = str(data.get("trace_id") or trace_id)
                events["meta"].append(data)
            elif current_event == "token":
                tokens.append(str(data.get("text") or ""))
            elif current_event in events:
                events[current_event].append(data)
            if current_event == "complete":
                break
    return {
        "ok": not events["error"],
        "status_code": 200,
        "latency_sec": round(time.monotonic() - started, 3),
        "trace_id": trace_id,
        "text": "".join(tokens),
        "event_counts": {k: len(v) for k, v in events.items()},
        "events": events,
    }


async def run(args: argparse.Namespace) -> Path:
    run_dir = RUNS_DIR / ("file_" + now_id())
    run_dir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(run_dir)
    base_url = args.base_url.rstrip("/")
    mats = prepare_materials("file_endurance_" + now_id())

    timeout = httpx.Timeout(connect=20, read=None, write=300, pool=20)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        health = await client.get(f"{base_url}/health")
        health.raise_for_status()
        archive = await client.post(
            f"{base_url}/v1/archives",
            json={"name": "file_endurance_" + now_id(), "persona_id": "environment"},
        )
        archive.raise_for_status()
        archive_id = archive.json()["archive_id"]
        group_id = "file_endurance_" + uuid.uuid4().hex[:8]

        await recorder.record({
            "kind": "run_start",
            "base_url": base_url,
            "archive_id": archive_id,
            "group_id": group_id,
            "materials": {k: str(v) for k, v in mats.items() if isinstance(v, Path)},
            "duration_min": args.duration_min,
        })

        end_at = time.monotonic() + args.duration_min * 60.0
        turn = 0
        while time.monotonic() < end_at and turn < args.max_turns:
            file_ids = [
                await upload_file(client, base_url=base_url, archive_id=archive_id, group_id=group_id, path=mats["txt"]),
                await upload_file(client, base_url=base_url, archive_id=archive_id, group_id=group_id, path=mats["csv"]),
                await upload_file(client, base_url=base_url, archive_id=archive_id, group_id=group_id, path=mats["image"]),
                await upload_file(client, base_url=base_url, archive_id=archive_id, group_id=group_id, path=mats["docx"]),
            ]
            prompt = (
                "我上传了 txt、csv、图片和 docx。请像真实助手一样处理："
                "识别图片里的订单编号、金额、校验词；读取 docx 暗号；计算 csv 收入合计；"
                "生成一个 markdown 总结文件作为产物；再生成一段很短的语音回复文件。"
            )
            await recorder.record({
                "kind": "turn_start",
                "turn": turn,
                "prompt": prompt,
                "file_ids": file_ids,
            })
            result = await stream_chat(
                client,
                base_url=base_url,
                archive_id=archive_id,
                group_id=group_id,
                user_id="file_endurance_user",
                user_name="FileEnduranceUser",
                message=prompt,
                attached_file_ids=[x for x in file_ids if x],
                timeout_sec=args.scenario_timeout_sec,
            )
            files = (await client.get(f"{base_url}/v1/chat/files/{archive_id}/{group_id}")).json()
            artifacts = (await client.get(f"{base_url}/v1/chat/artifacts/{archive_id}/{group_id}")).json()
            text = str(result.get("text") or "")
            checks = {
                "ocr_order": "ZX-2026-0617" in text or "0617" in text,
                "ocr_amount": "128.50" in text or "128" in text,
                "ocr_word": "kiwi42" in text.lower(),
                "csv_total": "316" in text,
                "has_summary_artifact": "summary.md" in json.dumps(artifacts, ensure_ascii=False),
                "has_voice_artifact": "voice_reply.wav" in json.dumps(artifacts, ensure_ascii=False),
            }
            await recorder.record({
                "kind": "turn_result",
                "turn": turn,
                "checks": checks,
                "files": files,
                "artifacts": artifacts,
                **result,
            })
            turn += 1
            await asyncio.sleep(args.gap_sec)

    summary = {
        "run_dir": str(run_dir),
        "base_url": base_url,
        "event_count": sum(1 for _ in (run_dir / "events.jsonl").open("r", encoding="utf-8")),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_dir


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--duration-min", type=float, default=240.0)
    p.add_argument("--max-turns", type=int, default=24)
    p.add_argument("--gap-sec", type=float, default=30.0)
    p.add_argument("--scenario-timeout-sec", type=float, default=2400.0)
    return p.parse_args()


if __name__ == "__main__":
    print(asyncio.run(run(parse_args())))
