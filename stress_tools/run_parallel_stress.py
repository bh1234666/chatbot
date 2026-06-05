from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
RUNS_DIR = ROOT / "runs" / "parallel"


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


async def wait_health(base_url: str, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        last = ""
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{base_url.rstrip('/')}/health")
                if r.status_code == 200:
                    return
                last = f"status={r.status_code}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.0)
    raise RuntimeError(f"service did not become healthy: {last}")


def start_service(run_dir: Path, host: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.setdefault("DEBUG_MODE", "true")
    env.setdefault("DEBUG_LOG_DIR", str(run_dir / "debug_logs"))
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    args = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    stdout = (run_dir / "server.out").open("w", encoding="utf-8", errors="replace")
    stderr = (run_dir / "server.err").open("w", encoding="utf-8", errors="replace")
    return subprocess.Popen(
        args,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=stdout,
        stderr=stderr,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )


def stop_service(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=20)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def run_child(name: str, args: list[str], run_dir: Path) -> dict:
    started = time.monotonic()
    out_path = run_dir / f"{name}.out"
    err_path = run_dir / f"{name}.err"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await proc.communicate()
    except asyncio.CancelledError:
        await terminate_child(proc)
        raise
    out_path.write_bytes(stdout_b)
    err_path.write_bytes(stderr_b)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return {
        "name": name,
        "returncode": proc.returncode,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }


async def terminate_child(proc: asyncio.subprocess.Process, timeout_sec: float = 20.0) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=timeout_sec)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def newest_run(base: Path) -> Path | None:
    if not base.exists():
        return None
    dirs = [p for p in base.iterdir() if p.is_dir()]
    return max(dirs, key=lambda p: p.stat().st_mtime) if dirs else None


def load_summary(path: Path | None) -> dict:
    if path is None:
        return {}
    summary = path / "summary.json"
    if not summary.exists():
        return {}
    try:
        return json.loads(summary.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def run(args: argparse.Namespace) -> Path:
    run_id = now_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{args.port}"
    proc = None
    try:
        if args.start_service:
            proc = start_service(run_dir, "127.0.0.1", args.port)
        await wait_health(base_url, args.health_timeout_sec)

        group_cmd = [
            sys.executable,
            "group_sim/run_group_sim.py",
            "--base-url",
            base_url,
            "--duration-min",
            str(args.duration_min),
            "--members",
            str(args.members),
            "--bot-prob",
            str(args.bot_prob),
            "--min-gap-sec",
            str(args.group_min_gap_sec),
            "--max-gap-sec",
            str(args.group_max_gap_sec),
            "--force-bot-every-sec",
            str(args.force_bot_every_sec),
            "--max-bot-inflight",
            str(args.group_max_bot_inflight),
            "--max-bot-backlog",
            str(args.group_max_bot_backlog),
            "--member-model",
            str(args.group_member_model),
            "--monitor-interval-sec",
            str(args.monitor_interval_sec),
            "--drain-sec",
            str(args.group_drain_sec),
        ]
        env_cmd = [
            sys.executable,
            "stress_tools/run_environment_maintenance.py",
            "--base-url",
            base_url,
            "--duration-min",
            str(args.duration_min),
            "--projects",
            str(args.env_projects),
            "--max-inflight",
            str(args.env_max_inflight),
            "--review-gap-sec",
            str(args.env_review_gap_sec),
            "--monitor-interval-sec",
            str(args.monitor_interval_sec),
            "--drain-sec",
            str(args.env_drain_sec),
        ]
        expected_sec = max(60.0, args.duration_min * 60 + args.child_grace_sec)
        child_tasks = [
            asyncio.create_task(run_child("group_sim", group_cmd, run_dir)),
            asyncio.create_task(run_child("environment_maintenance", env_cmd, run_dir)),
        ]
        try:
            results = await asyncio.wait_for(asyncio.gather(*child_tasks), timeout=expected_sec)
        except asyncio.TimeoutError:
            results = []
            for task, name in zip(child_tasks, ("group_sim", "environment_maintenance")):
                if task.done() and not task.cancelled():
                    try:
                        results.append(task.result())
                        continue
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        results.append({
                            "name": name,
                            "returncode": None,
                            "elapsed_sec": round(expected_sec, 3),
                            "stdout_tail": "",
                            "stderr_tail": f"child result collection failed: {type(e).__name__}: {e}",
                            "timeout": True,
                        })
                        continue
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    results.append({
                        "name": name,
                        "returncode": None,
                        "elapsed_sec": round(expected_sec, 3),
                        "stdout_tail": "",
                        "stderr_tail": f"child did not exit within grace window ({expected_sec:.0f}s)",
                        "timeout": True,
                    })
                else:
                    results.append({
                        "name": name,
                        "returncode": None,
                        "elapsed_sec": round(expected_sec, 3),
                        "stdout_tail": "",
                        "stderr_tail": f"child did not exit within grace window ({expected_sec:.0f}s)",
                        "timeout": True,
                    })
        group_run = newest_run(PROJECT_ROOT / "group_sim" / "runs")
        env_run = newest_run(ROOT / "runs" / "environment")
        report = {
            "run_id": run_id,
            "base_url": base_url,
            "duration_min": args.duration_min,
            "children": results,
            "group_run": str(group_run) if group_run else "",
            "environment_run": str(env_run) if env_run else "",
            "group_summary": load_summary(group_run),
            "environment_summary": load_summary(env_run),
            "server_returncode": proc.poll() if proc else None,
        }
        (run_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# Parallel Stress Report",
            "",
            f"- Run: {run_id}",
            f"- Duration min: {args.duration_min}",
            f"- Group run: {report['group_run']}",
            f"- Environment run: {report['environment_run']}",
            "",
            "## Child Processes",
            "",
        ]
        for child in results:
            lines.append(f"- {child['name']}: rc={child['returncode']} elapsed={child['elapsed_sec']}s")
            if child["stderr_tail"]:
                lines.append(f"  - stderr tail: `{child['stderr_tail'][:700].replace(chr(10), ' ')}`")
        lines.extend(["", "## Summaries", "", "```json", json.dumps({
            "group": report["group_summary"],
            "environment": report["environment_summary"],
        }, ensure_ascii=False, indent=2), "```", ""])
        (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    finally:
        if args.start_service:
            stop_service(proc)
    return run_dir


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--duration-min", type=float, default=60.0)
    p.add_argument("--port", type=int, default=8017)
    p.add_argument("--start-service", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--health-timeout-sec", type=float, default=60.0)
    p.add_argument("--members", type=int, default=12)
    p.add_argument("--bot-prob", type=float, default=0.12)
    p.add_argument("--group-min-gap-sec", type=float, default=6.0)
    p.add_argument("--group-max-gap-sec", type=float, default=18.0)
    p.add_argument("--force-bot-every-sec", type=float, default=300.0)
    p.add_argument("--group-max-bot-inflight", type=int, default=2)
    p.add_argument("--group-max-bot-backlog", type=int, default=64)
    p.add_argument("--group-member-model", choices=["mixed", "gpt55", "deepseek"], default="mixed")
    p.add_argument("--env-projects", type=int, default=4)
    p.add_argument("--env-max-inflight", type=int, default=2)
    p.add_argument("--env-review-gap-sec", type=float, default=30.0)
    p.add_argument("--group-drain-sec", type=float, default=180.0)
    p.add_argument("--env-drain-sec", type=float, default=900.0)
    p.add_argument("--monitor-interval-sec", type=float, default=15.0)
    p.add_argument("--child-grace-sec", type=float, default=900.0)
    return p.parse_args()


if __name__ == "__main__":
    run_dir = asyncio.run(run(parse_args()))
    print(run_dir)
