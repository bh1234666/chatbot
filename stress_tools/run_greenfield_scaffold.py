"""Greenfield multi-language scaffold short test (6.5).

Asks the orchestrator to bootstrap a tiny end-to-end project that exercises
multiple languages (Python service + a small C utility + an HTML/JS UI). The
goal is to verify that the framework→slice→assemble pipeline can land a
runnable scaffold inside the wall-cap. Generation only — no full benchmark.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _driver_common import (  # noqa: E402
    DoneCapture,
    ask_followup_about_delivery,
    read_sse_with_stall_cap,
)

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "stress_tools" / "runs"
API = "http://127.0.0.1:8000/v1/environment/stream"
WALL_CAP_SEC = 1800  # 30 minutes — generation is heavier than inventory
STALL_CAP_SEC = 120  # generation has long compile/test pauses

EXPECTED_NEEDLES = {
    "server_py": ("server.py",),
    "digit_format_c": ("digit_format.c",),
    "makefile": ("makefile",),
    "index_html": ("index.html",),
    "app_js": ("app.js",),
    "readme": ("readme.md",),
    "test_server_py": ("test_server.py",),
}


def main() -> int:
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    session_dir = RUN_DIR / f"greenfield_scaffold_{run_id}"
    work_dir = session_dir / "greenfield_project"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    sse_log = session_dir / "sse.jsonl"
    summary = session_dir / "summary.json"

    body = {
        "user_id": f"greenfield-{run_id}",
        "user_name": "LongTest",
        "message": (
            "Greenfield scaffold task. Build a small but runnable multi-language "
            "project named pomodoro_kit in the current working directory. "
            "Required components:\n"
            "1. Python: pomodoro_kit/server.py — a tiny FastAPI service exposing "
            "POST /timers (creates a timer with name+duration_sec) and GET /timers "
            "(lists current timers). Use an in-memory store. Include a __main__ "
            "block that runs uvicorn on 127.0.0.1:8765.\n"
            "2. C: pomodoro_kit/native/digit_format.c — a small helper that takes "
            "a seconds integer on argv[1] and prints MM:SS to stdout. Include a "
            "Makefile that builds it to native/digit_format(.exe).\n"
            "3. HTML/JS: pomodoro_kit/web/index.html + web/app.js — a minimal "
            "page that fetches GET /timers and shows them in a list, with an "
            "input form to POST /timers. No build step required.\n"
            "4. README.md at repo root with run instructions.\n"
            "5. tests/test_server.py with at least 2 pytest tests using FastAPI "
            "TestClient that hit POST and GET.\n"
            "Workflow: first lay down a framework contract (file inventory + "
            "interfaces), then fan out slice helpers per component, then verify "
            "the result. Acceptance: pytest -q in tests/ must pass; the C source "
            "must compile with the Makefile (do not run the binary, just verify "
            "compilation succeeds); index.html must reference app.js. "
            "中文概要：搭一个跨语言番茄钟脚手架（FastAPI + C 工具 + HTML/JS 页面），"
            "先骨架后分片再验证；pytest 通过、C 可编译、HTML 引用 JS。"
        ),
        "current_dir": str(work_dir),
        "persona_id": "environment",
        "archive_id": f"greenfield-{run_id}",
        "client_msg_id": f"greenfield-{run_id}",
    }

    req = urllib.request.Request(
        API,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    started = time.time()
    capture = DoneCapture()
    stream_result = None

    print(f"[greenfield] run={run_id}")
    print(f"[greenfield] work_dir={work_dir}")

    sse_fh = sse_log.open("w", encoding="utf-8")

    def _on_text(text: str, t: float) -> None:
        sse_fh.write(json.dumps({"t": t, "line": text}, ensure_ascii=False) + "\n")
        sse_fh.flush()
        capture.feed_line(text)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            stream_result = read_sse_with_stall_cap(
                resp,
                wall_cap_sec=WALL_CAP_SEC,
                stall_cap_sec=STALL_CAP_SEC,
                on_text=_on_text,
            )
        if stream_result.truncated_wall_cap:
            print(f"[greenfield] wall-cap hit at {stream_result.elapsed_sec:.1f}s")
        elif stream_result.truncated_stall_cap:
            print(
                f"[greenfield] stall-cap hit at {stream_result.elapsed_sec:.1f}s "
                f"(no SSE activity for {STALL_CAP_SEC}s)"
            )
        elif stream_result.stream_error:
            print(f"[greenfield] stream error: {stream_result.stream_error}")
        else:
            print(f"[greenfield] stream ended at {stream_result.elapsed_sec:.1f}s")
    finally:
        sse_fh.close()

    line_count = stream_result.line_count if stream_result else 0
    truncated = bool(
        stream_result
        and (stream_result.truncated_wall_cap or stream_result.truncated_stall_cap)
    )

    def _marker_hits(declared: list[dict]) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for marker, needles in EXPECTED_NEEDLES.items():
            hit = False
            for entry in declared:
                hay = " ".join(
                    str(entry.get(k) or "").lower()
                    for k in ("name", "rel_path", "local_path", "url")
                ).replace("\\", "/")
                if any(n in hay for n in needles):
                    hit = True
                    break
            out[marker] = hit
        return out

    declared = list(capture.done_files)
    expected_markers = _marker_hits(declared)
    coverage = sum(1 for v in expected_markers.values() if v)

    followup: dict | None = None
    if coverage < 5 and capture.archive_id:
        # LLM didn't claim everything in the first turn. Ask it directly:
        # which of the expected files did you actually deliver?
        missing = [k for k, v in expected_markers.items() if not v]
        print(f"[greenfield] only {coverage}/7 markers in done.files; asking LLM about: {missing}")
        followup = ask_followup_about_delivery(
            API,
            archive_id=capture.archive_id,
            user_id=body["user_id"],
            current_dir=str(work_dir),
            expected=", ".join(EXPECTED_NEEDLES[m][0] for m in missing),
            wall_cap_sec=240,
        )
        if followup.get("ok"):
            for entry in followup.get("files") or []:
                declared.append(entry)
            print(f"[greenfield] follow-up answer: {followup.get('answer_text','')[:280]}")
            expected_markers = _marker_hits(declared)
            coverage = sum(1 for v in expected_markers.values() if v)

    summary_data = {
        "run_id": run_id,
        "wall_cap_sec": WALL_CAP_SEC,
        "stall_cap_sec": STALL_CAP_SEC,
        "elapsed_sec": stream_result.elapsed_sec if stream_result else round(time.time() - started, 2),
        "last_activity_at": stream_result.last_activity_at if stream_result else None,
        "lines": line_count,
        "truncated": truncated,
        "truncated_kind": (
            "wall_cap" if stream_result and stream_result.truncated_wall_cap
            else "stall_cap" if stream_result and stream_result.truncated_stall_cap
            else None
        ),
        "archive_id": capture.archive_id,
        "expected_markers": expected_markers,
        "coverage": f"{coverage}/{len(expected_markers)}",
        "llm_declared_deliveries": declared,
        "followup": followup,
        "work_dir": str(work_dir),
        "sse_log": str(sse_log),
    }
    summary.write_text(
        json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[greenfield] summary written to {summary}")
    print(json.dumps({k: summary_data[k] for k in (
        "run_id", "elapsed_sec", "truncated", "expected_markers", "coverage"
    )}, ensure_ascii=False, indent=2))
    return 0 if coverage >= 5 else 1


if __name__ == "__main__":
    sys.exit(main())
