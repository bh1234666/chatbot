from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "stress_tools" / "runs"
API = "http://127.0.0.1:8000/v1/environment/stream"
RUN_ID = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
SESSION_DIR = RUN_DIR / f"three_project_{RUN_ID}"


TASKS = [
    {
        "name": "ielts_may_materials",
        "current_dir": ROOT / "5\u6708\u96c5\u601d",
        "message": (
            "\u67e5\u770b\u5f53\u524d\u76ee\u5f55\u4e0b\u7684\u6240\u6709\u6587\u4ef6\uff0c\u5e2e\u6211\u6574\u7406\u96c5\u601d\u5907\u8003\u5185\u5bb9\u3002"
            "\u5206\u542c\u529b\u3001\u9605\u8bfb\u3001\u5199\u4f5c\u3001\u53e3\u8bed\u56db\u79d1\u6574\u7406\uff1b\u6309\u91cd\u8981\u6027\u53ca\u7c7b\u522b\u5206\u7c7b\u3002"
            "\u8bcd\u6c47\u90e8\u5206\u4e2d\uff0c\u72ec\u7acb\u96f6\u6563\u51fa\u73b0\u7684\u662f\u6700\u91cd\u8981\u7684\u3002"
            "\u4f5c\u6587\u3001\u53e3\u8bed\u7b49\u5927\u6982\u7387\u7528\u5230\u7684\u6846\u67b6\u6a21\u677f\u7c7b\u522b\u5f80\u524d\u653e\u3002"
            "\u53e3\u8bed\u90e8\u5206\u5982\u679c\u53ea\u6709\u5173\u952e\u8bcd\u6216\u5173\u952e\u53e5\uff0c\u8bf7\u6269\u5199\u4e3a 6-6.5 \u5206\u6c34\u5e73\u8303\u4f8b\uff1b"
            "\u4f5c\u6587\u53ef\u4ee5\u8865\u5145\u90e8\u5206\u8303\u6587\u6216\u6bb5\u843d\u3002\u9700\u8981\u7684\u5730\u65b9\u505a\u4e2d\u82f1\u5bf9\u7167\u3002"
            "\u8bf7\u5148\u7528\u8bfb\u53d6\u7c7b helper \u5e76\u884c\u8986\u76d6\u6750\u6599\uff0c\u518d\u7efc\u5408\u8f93\u51fa\uff0c\u4e0d\u8981\u4e3b\u8fdb\u7a0b\u81ea\u5df1\u541e\u5165\u6240\u6709\u539f\u6587\u3002"
        ),
    },
    {
        "name": "engineering_management",
        "current_dir": ROOT / "\u7535\u5b50231\u5de5\u7a0b\u7ba1\u7406",
        "message": (
            "\u5206\u6790\u5f53\u524d\u5de5\u7a0b\u7ba1\u7406\u76ee\u5f55\u7684\u6240\u6709\u6587\u4ef6\uff0c\u5148\u505a\u76ee\u5f55\u4e0e\u6750\u6599\u7c7b\u578b\u76d8\u70b9\uff0c"
            "\u518d\u6309\u8bfe\u7a0b/\u4efb\u52a1/\u8868\u683c/\u6587\u6863/\u98ce\u9669\u4e0e\u5f85\u529e\u5206\u7c7b\u6574\u7406\u6210\u4e00\u4efd\u7ed3\u6784\u5316\u62a5\u544a\u3002"
            "\u5bf9 Office/PDF/\u56fe\u7247\u7b49\u6750\u6599\u8981\u5148\u5206\u6279\u8bfb\u53d6\u5e76\u5f62\u6210\u8bc1\u636e\uff1b"
            "\u8981\u660e\u786e\u54ea\u4e9b\u6587\u4ef6\u5df2\u8bfb\u3001\u54ea\u4e9b\u65e0\u6cd5\u8bfb\u53d6\u3001\u7406\u7531\u662f\u4ec0\u4e48\uff0c\u6700\u540e\u7ed9\u51fa\u7ba1\u7406\u5efa\u8bae\u548c\u4e0b\u4e00\u6b65\u884c\u52a8\u6e05\u5355\u3002"
        ),
    },
    {
        "name": "db_index_paper_docx",
        "current_dir": SESSION_DIR / "db_index_paper_project",
        "message": (
            "\u6bd4\u8f83\u7ea2\u9ed1\u6811\u3001\u8df3\u8868\u3001B\u6811\u3001B+\u6811\u7b49\u6570\u636e\u7ed3\u6784\u7b97\u6cd5\uff0c"
            "\u5e76\u53d1\u660e\u4e00\u79cd\u65b0\u7684\u6570\u636e\u7ed3\u6784\u7b97\u6cd5\uff0c\u4e3a\u5176\u7f16\u5199\u4e00\u7bc7\u4e25\u8c28\u7684\u8bba\u6587\uff0cWord \u683c\u5f0f\u8f93\u51fa\u3002"
            "\u8bf7\u5148\u5efa\u7acb\u8bba\u6587\u6846\u67b6\u3001\u7b97\u6cd5\u6bd4\u8f83\u6807\u51c6\u548c\u9a8c\u6536\u6e05\u5355\uff0c"
            "\u518d\u5e76\u884c\u5b8c\u6210\u7b97\u6cd5\u5206\u6790\u3001\u8868\u683c\u548c\u6587\u6863\u5199\u4f5c\uff0c\u6700\u7ec8\u9a8c\u8bc1 docx \u6bb5\u843d\u3001\u8868\u683c\u548c\u683c\u5f0f\u8d28\u91cf\u3002"
        ),
    },
]


def stream_task(task: dict) -> None:
    current_dir = Path(task["current_dir"])
    if task["name"] == "db_index_paper_docx" and current_dir.exists():
        shutil.rmtree(current_dir)
    current_dir.mkdir(parents=True, exist_ok=True)
    out = RUN_DIR / f"{task['name']}_sse.jsonl"
    session_out = SESSION_DIR / f"{task['name']}_sse.jsonl"
    body = {
        "user_id": f"longtest-{RUN_ID}-{task['name']}",
        "user_name": "LongTest",
        "message": task["message"],
        "current_dir": str(current_dir),
        "persona_id": "environment",
        "archive_id": f"longtest-{RUN_ID}-{task['name']}",
        "client_msg_id": f"{task['name']}-{RUN_ID}-{int(time.time())}",
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp, out.open("w", encoding="utf-8") as fh, session_out.open("w", encoding="utf-8") as sfh:
        while True:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            if text:
                record = json.dumps({"t": round(time.time() - start, 2), "line": text}, ensure_ascii=False) + "\n"
                fh.write(record)
                fh.flush()
                sfh.write(record)
                sfh.flush()


def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / "run_id.txt").write_text(RUN_ID, encoding="utf-8")
    threads: list[threading.Thread] = []
    for task in TASKS:
        t = threading.Thread(target=stream_task, args=(task,), daemon=False)
        t.start()
        threads.append(t)
        time.sleep(2)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
