from __future__ import annotations

import json
import shutil
import time
import urllib.request
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "stress_tools" / "runs"
API = "http://127.0.0.1:8000/v1/environment/stream"


def main() -> None:
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    session_dir = RUN_DIR / f"db_paper_smoke_{run_id}"
    work_dir = session_dir / "db_index_paper_project"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out = session_dir / "db_index_paper_sse.jsonl"
    body = {
        "user_id": f"db-smoke-{run_id}",
        "user_name": "LongTest",
        "message": (
            "比较红黑树、跳表、B树、B+树等数据结构算法，并发明一种新的数据结构算法，"
            "为其编写一篇严谨的论文，Word 格式输出。请先建立框架契约，再把算法实现、"
            "基准数据、理论表格和文档写作拆成可验证的 helper 分片。"
        ),
        "current_dir": str(work_dir),
        "persona_id": "environment",
        "archive_id": f"db-smoke-{run_id}",
        "client_msg_id": f"db-smoke-{run_id}",
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp, out.open("w", encoding="utf-8") as fh:
        while True:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            if text:
                fh.write(json.dumps({"t": round(time.time() - start, 2), "line": text}, ensure_ascii=False) + "\n")
                fh.flush()


if __name__ == "__main__":
    main()
