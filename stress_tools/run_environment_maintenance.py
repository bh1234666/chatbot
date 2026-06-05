from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import uuid
import codecs
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from textwrap import dedent
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from stress_tools.response_quality import ineffective_reply_reasons, is_effective_success


ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs" / "environment"


async def _iter_utf8_sse_lines(response: httpx.Response):
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        pending += decoder.decode(chunk)
        while True:
            marker = pending.find("\n")
            if marker < 0:
                break
            line = pending[:marker]
            pending = pending[marker + 1:]
            yield line.rstrip("\r")
    tail = pending + decoder.decode(b"", final=True)
    if tail:
        yield tail.rstrip("\r")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class EnvRecorder:
    run_dir: Path
    events_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.events_path = self.run_dir / "events.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    async def record(self, event: dict[str, Any]) -> None:
        event = {"ts": now_iso(), **event}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


class EnvironmentClient:
    def __init__(self, base_url: str, recorder: EnvRecorder):
        self.base_url = base_url.rstrip("/")
        self.recorder = recorder
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=20.0), trust_env=False)

    async def close(self) -> None:
        await self.client.aclose()

    async def health(self) -> dict:
        r = await self.client.get(f"{self.base_url}/health")
        r.raise_for_status()
        return r.json()

    async def active(self) -> dict:
        r = await self.client.get(f"{self.base_url}/v1/environment/active")
        r.raise_for_status()
        return r.json()

    async def active_commands(self) -> dict:
        r = await self.client.get(f"{self.base_url}/v1/environment/commands/active")
        r.raise_for_status()
        return r.json()

    async def monitor_sample(self, duration_sec: float = 5.0) -> list[dict]:
        events: list[dict] = []
        deadline = time.monotonic() + duration_sec
        try:
            async with self.client.stream(
                "GET",
                f"{self.base_url}/v1/environment/monitor",
                params={"heartbeat_sec": 2},
            ) as r:
                r.encoding = "utf-8"
                r.raise_for_status()
                current_event = "message"
                data_lines: list[str] = []
                async for line in _iter_utf8_sse_lines(r):
                    if time.monotonic() >= deadline:
                        break
                    if line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                data = json.loads(raw)
                            except Exception:
                                data = {"raw": raw}
                            events.append({"event": current_event, "data": data})
                        current_event = "message"
                        data_lines = []
                        continue
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].strip())
        except Exception as e:
            await self.recorder.record({"kind": "monitor_error", "error": f"{type(e).__name__}: {e}"})
        return events

    async def ask_environment(
        self,
        *,
        user_id: str,
        current_dir: Path,
        message: str,
        client_msg_id: str,
    ) -> dict:
        payload = {
            "user_id": user_id,
            "user_name": user_id,
            "message": message,
            "current_dir": str(current_dir),
            "client_msg_id": client_msg_id,
        }
        started = time.monotonic()
        tokens: list[str] = []
        progress: list[dict] = []
        workflow: list[dict] = []
        command_events: list[dict] = []
        errors: list[dict] = []
        meta: dict[str, Any] = {}
        done: dict[str, Any] = {}
        try:
            async with self.client.stream("POST", f"{self.base_url}/v1/environment/stream", json=payload) as r:
                r.encoding = "utf-8"
                if r.status_code >= 400:
                    body = await r.aread()
                    return {
                        "ok": False,
                        "status_code": r.status_code,
                        "latency_sec": time.monotonic() - started,
                        "error": body.decode("utf-8", errors="replace")[:2000],
                    }
                current_event = "message"
                data_lines: list[str] = []
                async for line in _iter_utf8_sse_lines(r):
                    if line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                data = json.loads(raw)
                            except Exception:
                                data = {"raw": raw}
                            if current_event == "token":
                                tokens.append(str(data.get("text") or ""))
                            elif current_event == "progress":
                                progress.append(data)
                            elif current_event == "workflow":
                                workflow.append(data)
                            elif current_event == "command":
                                command_events.append(data)
                            elif current_event == "meta":
                                meta = data
                            elif current_event == "done":
                                done = data
                            elif current_event == "error":
                                errors.append(data)
                            elif current_event == "complete":
                                break
                        current_event = "message"
                        data_lines = []
                        continue
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].strip())
        except Exception as e:
            return {
                "ok": False,
                "status_code": 0,
                "latency_sec": time.monotonic() - started,
                "error": f"{type(e).__name__}: {e}",
            }
        text = "".join(tokens).strip()
        quality_fail_reasons = environment_quality_fail_reasons(text)
        return {
            "ok": not errors,
            "quality_fail_reasons": quality_fail_reasons,
            "status_code": 200,
            "latency_sec": time.monotonic() - started,
            "trace_id": meta.get("trace_id") or done.get("trace_id"),
            "text": text,
            "progress": progress,
            "workflow": workflow,
            "command_events": command_events,
            "errors": errors,
            "done": done,
        }


def environment_quality_fail_reasons(text: str) -> list[str]:
    reasons = ineffective_reply_reasons(text)
    if "_env/" in (text or "").replace("\\", "/"):
        reasons.append("internal_env_staging_path_in_final_reply")
    return reasons


def _write_project_files(project: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text).lstrip("\n"), encoding="utf-8")


def _taskboard_files(index: int) -> dict[str, str]:
    return {
        "README.md": (
            f"# TaskBoard Maintenance Project {index}\n\n"
            "Goal: turn this scaffold into a usable local task-board CLI and library. "
            "The project deliberately starts with many files so environment mode must maintain a real tree.\n\n"
            "Run checks with `python -m compileall src tests` and `set PYTHONPATH=src&& python -m taskboard --help` after implementation.\n"
        ),
        "pyproject.toml": (
            "[project]\n"
            f"name = \"taskboard-maintenance-{index}\"\n"
            "version = \"0.1.0\"\n"
            "description = \"Local task board CLI used by environment-mode stress tests\"\n"
            "requires-python = \">=3.10\"\n\n"
            "[project.scripts]\n"
            "taskboard = \"taskboard.cli:main\"\n\n"
            "[tool.pytest.ini_options]\n"
            "testpaths = [\"tests\"]\n"
        ),
        ".gitignore": "__pycache__/\n*.pyc\n.taskboard.json\n.coverage\n",
        "src/taskboard/__init__.py": "__version__ = \"0.1.0\"\n",
        "src/taskboard/__main__.py": "from .cli import main\n\nif __name__ == \"__main__\":\n    raise SystemExit(main())\n",
        "src/taskboard/models.py": (
            "from dataclasses import dataclass, field\n"
            "from datetime import datetime\n\n"
            "@dataclass\n"
            "class Task:\n"
            "    title: str\n"
            "    status: str = \"todo\"\n"
            "    priority: int = 2\n"
            "    tags: list[str] = field(default_factory=list)\n"
            "    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec=\"seconds\"))\n"
        ),
        "src/taskboard/storage.py": (
            "from pathlib import Path\n\n"
            "DEFAULT_DB = Path('.taskboard.json')\n\n"
            "def load_tasks(path: Path = DEFAULT_DB):\n"
            "    return []\n\n"
            "def save_tasks(tasks, path: Path = DEFAULT_DB):\n"
            "    path.write_text('[]\\n', encoding='utf-8')\n"
        ),
        "src/taskboard/cli.py": (
            "import argparse\n\n"
            "def build_parser():\n"
            "    parser = argparse.ArgumentParser(prog='taskboard')\n"
            "    parser.add_argument('--version', action='store_true')\n"
            "    return parser\n\n"
            "def main(argv=None):\n"
            "    args = build_parser().parse_args(argv)\n"
            "    if args.version:\n"
            "        print('taskboard 0.1.0')\n"
            "    else:\n"
            "        print('TaskBoard scaffold')\n"
            "    return 0\n"
        ),
        "src/taskboard/analytics.py": "def summarize(tasks):\n    return {'total': len(tasks)}\n",
        "src/taskboard/filters.py": "def by_status(tasks, status):\n    return [t for t in tasks if getattr(t, 'status', None) == status]\n",
        "src/taskboard/formatters.py": "def as_table(tasks):\n    return '\\n'.join(str(t) for t in tasks)\n",
        "src/taskboard/importers.py": "def import_lines(text):\n    return [line.strip() for line in text.splitlines() if line.strip()]\n",
        "src/taskboard/exporters.py": "def export_markdown(tasks):\n    return '\\n'.join(f'- {t}' for t in tasks)\n",
        "src/taskboard/validation.py": "def validate_status(status):\n    return status in {'todo', 'doing', 'done', 'blocked'}\n",
        "src/taskboard/config.py": "DEFAULT_STATUSES = ['todo', 'doing', 'done', 'blocked']\n",
        "src/taskboard/errors.py": "class TaskBoardError(Exception):\n    pass\n",
        "src/taskboard/sample_data.py": "SAMPLE_TASKS = ['write docs', 'add tests', 'ship cli']\n",
        "tests/__init__.py": "",
        "tests/test_cli.py": (
            "from taskboard.cli import main\n\n"
            "def test_main_version(capsys):\n"
            "    assert main(['--version']) == 0\n"
            "    assert 'taskboard' in capsys.readouterr().out.lower()\n"
        ),
        "tests/test_models.py": "from taskboard.models import Task\n\ndef test_task_defaults():\n    assert Task('x').status == 'todo'\n",
        "tests/test_storage.py": "def test_storage_placeholder():\n    assert True\n",
        "tests/test_filters.py": "def test_filters_placeholder():\n    assert True\n",
        "tests/test_analytics.py": "def test_analytics_placeholder():\n    assert True\n",
        "docs/overview.md": "# Overview\n\nTaskBoard is a local planning tool.\n",
        "docs/cli.md": "# CLI\n\nDocument commands here.\n",
        "docs/data-format.md": "# Data Format\n\nDocument the JSON schema here.\n",
        "docs/maintenance-log.md": "# Maintenance Log\n\nSupervisor notes will be reflected here.\n",
        "data/samples/tasks.json": "[{\"title\":\"draft roadmap\",\"status\":\"todo\",\"priority\":2}]\n",
        "data/samples/import.txt": "write parser\nadd export command\nreview docs\n",
        "data/fixtures/empty.json": "[]\n",
        "config/statuses.json": "[\"todo\", \"doing\", \"done\", \"blocked\"]\n",
        "scripts/check_project.py": (
            "from pathlib import Path\n"
            "required = ['src/taskboard/cli.py', 'src/taskboard/storage.py', 'README.md']\n"
            "missing = [p for p in required if not Path(p).exists()]\n"
            "if missing:\n"
            "    raise SystemExit('missing: ' + ', '.join(missing))\n"
            "print('project structure ok')\n"
        ),
        "scripts/dev_notes.md": "# Dev Notes\n\nUse small, verified changes.\n",
        "examples/basic_usage.md": "# Basic Usage\n\nExamples will be added by the agent.\n",
        "examples/sample_export.md": "# Sample Export\n\nNo export yet.\n",
    }


def _snake_files(index: int) -> dict[str, str]:
    return {
        "README.md": f"""
        # Snake Arcade Maintenance Project {index}

        Goal: build a runnable terminal Snake game with isolated game logic, CLI entrypoint,
        tests, docs, and room for additional modes.
        """,
        "pyproject.toml": f"""
        [project]
        name = "snake-arcade-maintenance-{index}"
        version = "0.1.0"
        requires-python = ">=3.10"

        [project.scripts]
        snake-arcade = "snake_arcade.cli:main"

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """,
        ".gitignore": "__pycache__/\n*.pyc\n.coverage\n.scores.json\n",
        "src/snake_arcade/__init__.py": "__version__ = '0.1.0'\n",
        "src/snake_arcade/__main__.py": "from .cli import main\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
        "src/snake_arcade/cli.py": """
        import argparse

        def build_parser():
            parser = argparse.ArgumentParser(prog='snake-arcade')
            parser.add_argument('--version', action='store_true')
            return parser

        def main(argv=None):
            args = build_parser().parse_args(argv)
            if args.version:
                print('snake-arcade 0.1.0')
            else:
                print('Snake scaffold')
            return 0
        """,
        "src/snake_arcade/game.py": "class SnakeGame:\n    def __init__(self):\n        self.score = 0\n",
        "src/snake_arcade/board.py": "DEFAULT_WIDTH = 20\nDEFAULT_HEIGHT = 12\n",
        "src/snake_arcade/entities.py": "DIRECTIONS = {'up': (0, -1), 'down': (0, 1), 'left': (-1, 0), 'right': (1, 0)}\n",
        "src/snake_arcade/render.py": "def render_text(game):\n    return 'snake'\n",
        "src/snake_arcade/input.py": "def normalize_key(key):\n    return key.lower().strip()\n",
        "src/snake_arcade/storage.py": "def load_scores(path):\n    return []\n",
        "src/snake_arcade/modes.py": "MODES = ['classic']\n",
        "src/snake_arcade/config.py": "DEFAULT_TICK_RATE = 8\n",
        "src/snake_arcade/errors.py": "class SnakeError(Exception):\n    pass\n",
        "tests/__init__.py": "",
        "tests/test_cli.py": "from snake_arcade.cli import main\n\ndef test_version(capsys):\n    assert main(['--version']) == 0\n    assert 'snake-arcade' in capsys.readouterr().out\n",
        "tests/test_game.py": "def test_placeholder():\n    assert True\n",
        "tests/test_board.py": "def test_placeholder_board():\n    assert True\n",
        "tests/test_render.py": "def test_placeholder_render():\n    assert True\n",
        "docs/design.md": "# Design\n\nDocument gameplay rules here.\n",
        "docs/controls.md": "# Controls\n\nDocument keyboard controls here.\n",
        "docs/modes.md": "# Modes\n\nClassic mode exists first.\n",
        "docs/maintenance-log.md": "# Maintenance Log\n\n",
        "assets/levels/classic.txt": "####################\n#.................F#\n#.................S#\n####################\n",
        "assets/themes/default.json": "{\"snake\":\"green\",\"food\":\"red\"}\n",
        "examples/manual_play.md": "# Manual Play\n\n",
        "examples/ai_strategy.md": "# Strategy Notes\n\n",
        "scripts/check_project.py": "from pathlib import Path\nrequired=['src/snake_arcade/cli.py','src/snake_arcade/game.py','README.md']\nmissing=[p for p in required if not Path(p).exists()]\nif missing: raise SystemExit('missing: '+', '.join(missing))\nprint('snake structure ok')\n",
        "config/game.json": "{\"width\":20,\"height\":12,\"tick_rate\":8}\n",
        "data/scores/sample.json": "[]\n",
    }


def _dataset_files(index: int) -> dict[str, str]:
    return {
        "README.md": f"""
        # DatasetOps Maintenance Project {index}

        Goal: maintain a small structured dataset pipeline with validation, summary reports,
        documentation, tests, and reproducible command-line checks.
        """,
        "pyproject.toml": f"""
        [project]
        name = "datasetops-maintenance-{index}"
        version = "0.1.0"
        requires-python = ">=3.10"

        [project.scripts]
        datasetops = "datasetops.cli:main"

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """,
        ".gitignore": "__pycache__/\n*.pyc\n.coverage\nreports/*.tmp\n",
        "src/datasetops/__init__.py": "__version__ = '0.1.0'\n",
        "src/datasetops/__main__.py": "from .cli import main\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
        "src/datasetops/cli.py": """
        import argparse

        def build_parser():
            parser = argparse.ArgumentParser(prog='datasetops')
            parser.add_argument('--version', action='store_true')
            return parser

        def main(argv=None):
            args = build_parser().parse_args(argv)
            if args.version:
                print('datasetops 0.1.0')
            else:
                print('DatasetOps scaffold')
            return 0
        """,
        "src/datasetops/loader.py": "def load_rows(path):\n    return []\n",
        "src/datasetops/schema.py": "REQUIRED_FIELDS = ['id', 'name', 'category', 'value']\n",
        "src/datasetops/validate.py": "def validate_rows(rows):\n    return []\n",
        "src/datasetops/summary.py": "def summarize(rows):\n    return {'rows': len(rows)}\n",
        "src/datasetops/export.py": "def export_markdown(summary):\n    return '# Summary\\n'\n",
        "src/datasetops/clean.py": "def normalize_name(value):\n    return value.strip()\n",
        "src/datasetops/storage.py": "def write_json(path, data):\n    path.write_text('[]\\n', encoding='utf-8')\n",
        "src/datasetops/filters.py": "def by_category(rows, category):\n    return [r for r in rows if r.get('category') == category]\n",
        "src/datasetops/errors.py": "class DatasetOpsError(Exception):\n    pass\n",
        "tests/__init__.py": "",
        "tests/test_cli.py": "from datasetops.cli import main\n\ndef test_version(capsys):\n    assert main(['--version']) == 0\n    assert 'datasetops' in capsys.readouterr().out\n",
        "tests/test_validate.py": "def test_placeholder_validate():\n    assert True\n",
        "tests/test_summary.py": "def test_placeholder_summary():\n    assert True\n",
        "tests/test_loader.py": "def test_placeholder_loader():\n    assert True\n",
        "tests/test_export.py": "def test_placeholder_export():\n    assert True\n",
        "docs/schema.md": "# Schema\n\n",
        "docs/pipeline.md": "# Pipeline\n\n",
        "docs/reports.md": "# Reports\n\n",
        "docs/quality-gates.md": "# Quality Gates\n\n",
        "docs/maintenance-log.md": "# Maintenance Log\n\n",
        "data/raw/sample.csv": "id,name,category,value\n1,Ada,alpha,10\n2,Bob,beta,7\n",
        "data/raw/bad_rows.csv": "id,name,category,value\n3,,alpha,not-a-number\n",
        "data/processed/.gitkeep": "",
        "reports/.gitkeep": "",
        "scripts/check_project.py": "from pathlib import Path\nrequired=['src/datasetops/cli.py','src/datasetops/validate.py','data/raw/sample.csv','README.md']\nmissing=[p for p in required if not Path(p).exists()]\nif missing: raise SystemExit('missing: '+', '.join(missing))\nprint('dataset structure ok')\n",
        "config/pipeline.json": "{\"input\":\"data/raw/sample.csv\",\"report\":\"reports/summary.md\"}\n",
        "examples/report_example.md": "# Example Report\n\n",
    }


def _multilang_files(index: int) -> dict[str, str]:
    return {
        "README.md": f"""
        # PolyBench Maintenance Project {index}

        Goal: maintain a mixed-language repository with Python orchestration, a C sorting
        library, a C++ graph module, small JavaScript utilities, docs, data fixtures, and
        reproducible build checks. The project intentionally starts broad so environment mode
        must map the tree before editing.

        Primary verification: `python scripts/check_project.py`.
        """,
        ".gitignore": """
        __pycache__/
        *.pyc
        build/
        dist/
        node_modules/
        .pytest_cache/
        reports/*.tmp
        """,
        "pyproject.toml": f"""
        [project]
        name = "polybench-maintenance-{index}"
        version = "0.1.0"
        requires-python = ">=3.10"

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """,
        "Makefile": """
        PYTHON ?= python

        check:
        	$(PYTHON) scripts/check_project.py
        """,
        "src/polybench/__init__.py": "__version__ = '0.1.0'\n",
        "src/polybench/__main__.py": "from .cli import main\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
        "src/polybench/cli.py": """
        import argparse

        def build_parser():
            parser = argparse.ArgumentParser(prog='polybench')
            parser.add_argument('--version', action='store_true')
            parser.add_argument('--check-data', action='store_true')
            return parser

        def main(argv=None):
            args = build_parser().parse_args(argv)
            if args.version:
                print('polybench 0.1.0')
            elif args.check_data:
                print('data check scaffold')
            else:
                print('PolyBench scaffold')
            return 0
        """,
        "src/polybench/datasets.py": "def load_numbers(path):\n    return [int(x) for x in path.read_text().split() if x.strip()]\n",
        "src/polybench/reports.py": "def render_summary(stats):\n    return '# PolyBench Summary\\n\\nPending implementation.\\n'\n",
        "src/polybench/metrics.py": "def mean(values):\n    return sum(values) / len(values) if values else 0\n",
        "src/polybench/interop.py": "def native_binary_name():\n    return 'sortbench.exe'\n",
        "src/polybench/config.py": "DEFAULT_DATASET = 'data/numbers/small.txt'\n",
        "src/polybench/errors.py": "class PolyBenchError(Exception):\n    pass\n",
        "native/include/sortlib.h": """
        #ifndef SORTLIB_H
        #define SORTLIB_H
        #include <stddef.h>
        int is_sorted_ints(const int *values, size_t count);
        void insertion_sort_ints(int *values, size_t count);
        int parse_ints(const char *text, int *out, size_t max_count);
        #endif
        """,
        "native/src/sortlib.c": """
        #include "sortlib.h"
        #include <stdlib.h>
        #include <string.h>

        int is_sorted_ints(const int *values, size_t count) {
            for (size_t i = 1; i < count; ++i) {
                if (values[i - 1] > values[i]) {
                    return 0;
                }
            }
            return 1;
        }

        void insertion_sort_ints(int *values, size_t count) {
            for (size_t i = 1; i < count; ++i) {
                int key = values[i];
                size_t j = i;
                while (j > 0 && values[j - 1] > key) {
                    values[j] = values[j - 1];
                    --j;
                }
                values[j] = key;
            }
        }

        int parse_ints(const char *text, int *out, size_t max_count) {
            size_t count = 0;
            const char *cursor = text;
            while (*cursor && count < max_count) {
                char *end = NULL;
                long value = strtol(cursor, &end, 10);
                if (end == cursor) {
                    ++cursor;
                    continue;
                }
                out[count++] = (int)value;
                cursor = end;
            }
            return (int)count;
        }
        """,
        "native/src/sortbench.c": """
        #include "sortlib.h"
        #include <stdio.h>

        int main(void) {
            int values[] = {5, 1, 4, 2, 8};
            size_t count = sizeof(values) / sizeof(values[0]);
            insertion_sort_ints(values, count);
            if (!is_sorted_ints(values, count)) {
                fprintf(stderr, "sort failed\\n");
                return 2;
            }
            printf("sortbench ok\\n");
            return 0;
        }
        """,
        "cpp/include/graph.hpp": """
        #pragma once
        #include <string>
        #include <vector>

        namespace polybench {
        struct Edge { std::string from; std::string to; int weight; };
        int total_weight(const std::vector<Edge>& edges);
        }
        """,
        "cpp/src/graph.cpp": """
        #include "graph.hpp"

        namespace polybench {
        int total_weight(const std::vector<Edge>& edges) {
            int total = 0;
            for (const auto& edge : edges) {
                total += edge.weight;
            }
            return total;
        }
        }
        """,
        "cpp/src/graph_demo.cpp": """
        #include "graph.hpp"
        #include <iostream>
        #include <vector>

        int main() {
            std::vector<polybench::Edge> edges = {{"a", "b", 2}, {"b", "c", 3}};
            std::cout << "graph weight " << polybench::total_weight(edges) << "\\n";
            return polybench::total_weight(edges) == 5 ? 0 : 3;
        }
        """,
        "js/package.json": """
        {
          "name": "polybench-utils",
          "version": "0.1.0",
          "type": "module",
          "scripts": {
            "check": "node --check src/format.js && node src/format.js"
          }
        }
        """,
        "js/src/format.js": """
        export function formatMetric(name, value) {
          return `${name}: ${Number(value).toFixed(2)}`;
        }

        if (import.meta.url === `file://${process.argv[1]}`) {
          console.log(formatMetric('mean', 3.5));
        }
        """,
        "js/src/table.js": "export function rowsToTable(rows) { return rows.map((r) => r.join(',')).join('\\n'); }\n",
        "tests/test_cli.py": "from polybench.cli import main\n\ndef test_version(capsys):\n    assert main(['--version']) == 0\n    assert 'polybench' in capsys.readouterr().out\n",
        "tests/test_metrics.py": "from polybench.metrics import mean\n\ndef test_mean():\n    assert mean([1, 2, 3]) == 2\n",
        "tests/test_datasets.py": "def test_dataset_placeholder():\n    assert True\n",
        "tests/test_interop.py": "from polybench.interop import native_binary_name\n\ndef test_native_binary_name():\n    assert native_binary_name().endswith('.exe')\n",
        "scripts/check_project.py": """
        from __future__ import annotations
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[1]

        required = [
            'src/polybench/cli.py',
            'native/include/sortlib.h',
            'native/src/sortlib.c',
            'native/src/sortbench.c',
            'cpp/include/graph.hpp',
            'cpp/src/graph.cpp',
            'js/src/format.js',
            'docs/build.md',
            'data/numbers/small.txt',
        ]
        missing = [p for p in required if not (ROOT / p).exists()]
        if missing:
            raise SystemExit('missing: ' + ', '.join(missing))
        print('polybench structure ok; TODO: add compile/import/native/js checks')
        """,
        "scripts/profile_data.py": """
        from pathlib import Path
        values = [int(x) for x in Path('data/numbers/small.txt').read_text().split()]
        print({'count': len(values), 'min': min(values), 'max': max(values)})
        """,
        "scripts/generate_report.py": "from pathlib import Path\nPath('reports/summary.md').write_text('# Summary\\n\\nPending implementation.\\n', encoding='utf-8')\n",
        "docs/build.md": "# Build\n\nRun `python scripts/check_project.py` from the project root.\n",
        "docs/architecture.md": "# Architecture\n\nPython orchestrates C/C++/JS components through explicit checks.\n",
        "docs/native.md": "# Native Modules\n\nC sorting and C++ graph demos are compiled during verification.\n",
        "docs/js-utils.md": "# JS Utilities\n\nSmall dependency-free utilities live under `js/src`.\n",
        "docs/data-contract.md": "# Data Contract\n\nNumber fixtures are whitespace-separated integers.\n",
        "docs/maintenance-log.md": "# Maintenance Log\n\n",
        "data/numbers/small.txt": "5 1 4 2 8 13 21 3\n",
        "data/numbers/duplicates.txt": "7 7 2 2 9 1 1\n",
        "data/graphs/simple_edges.csv": "from,to,weight\na,b,2\nb,c,3\n",
        "config/build_profiles.json": "{\"default\":{\"cstd\":\"c11\",\"cppstd\":\"c++17\"}}\n",
        "config/report.json": "{\"input\":\"data/numbers/small.txt\",\"output\":\"reports/summary.md\"}\n",
        "examples/native_usage.md": "# Native Usage\n\nCompile via `scripts/check_project.py`.\n",
        "examples/report.md": "# Report Example\n\n",
        "reports/.gitkeep": "",
    }


def project_kind_for_index(index: int, selected: tuple[str, ...] | None = None) -> str:
    kinds = selected or ("taskboard", "snake", "dataset", "multilang")
    return kinds[index % len(kinds)]


def create_project(root: Path, index: int, selected_kinds: tuple[str, ...] | None = None) -> Path:
    kind = project_kind_for_index(index, selected_kinds)
    if kind == "snake":
        project = root / f"project_{index:02d}_snake_arcade"
        files = _snake_files(index)
    elif kind == "dataset":
        project = root / f"project_{index:02d}_datasetops"
        files = _dataset_files(index)
    elif kind == "multilang":
        project = root / f"project_{index:02d}_polybench"
        files = _multilang_files(index)
    else:
        project = root / f"project_{index:02d}_taskboard"
        files = _taskboard_files(index)

    if project.exists():
        return project
    project.mkdir(parents=True, exist_ok=True)
    _write_project_files(project, files)
    return project


def create_legacy_taskboard_project(root: Path, index: int) -> Path:
    project = root / f"project_{index:02d}_taskboard"
    if project.exists():
        return project
    project.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {
        "README.md": (
            f"# TaskBoard Maintenance Project {index}\n\n"
            "Goal: turn this scaffold into a usable local task-board CLI and library. "
            "The project deliberately starts with many files so environment mode must maintain a real tree.\n\n"
            "Run checks with `python -m compileall src tests` and `set PYTHONPATH=src&& python -m taskboard --help` after implementation.\n"
        ),
        "pyproject.toml": (
            "[project]\n"
            f"name = \"taskboard-maintenance-{index}\"\n"
            "version = \"0.1.0\"\n"
            "description = \"Local task board CLI used by environment-mode stress tests\"\n"
            "requires-python = \">=3.10\"\n\n"
            "[project.scripts]\n"
            "taskboard = \"taskboard.cli:main\"\n\n"
            "[tool.pytest.ini_options]\n"
            "testpaths = [\"tests\"]\n"
        ),
        ".gitignore": "__pycache__/\n*.pyc\n.taskboard.json\n.coverage\n",
        "src/taskboard/__init__.py": "__version__ = \"0.1.0\"\n",
        "src/taskboard/__main__.py": "from .cli import main\n\nif __name__ == \"__main__\":\n    raise SystemExit(main())\n",
        "src/taskboard/models.py": (
            "from dataclasses import dataclass, field\n"
            "from datetime import datetime\n\n"
            "@dataclass\n"
            "class Task:\n"
            "    title: str\n"
            "    status: str = \"todo\"\n"
            "    priority: int = 2\n"
            "    tags: list[str] = field(default_factory=list)\n"
            "    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec=\"seconds\"))\n"
        ),
        "src/taskboard/storage.py": (
            "from pathlib import Path\n\n"
            "DEFAULT_DB = Path('.taskboard.json')\n\n"
            "def load_tasks(path: Path = DEFAULT_DB):\n"
            "    return []\n\n"
            "def save_tasks(tasks, path: Path = DEFAULT_DB):\n"
            "    path.write_text('[]\\n', encoding='utf-8')\n"
        ),
        "src/taskboard/cli.py": (
            "import argparse\n\n"
            "def build_parser():\n"
            "    parser = argparse.ArgumentParser(prog='taskboard')\n"
            "    parser.add_argument('--version', action='store_true')\n"
            "    return parser\n\n"
            "def main(argv=None):\n"
            "    args = build_parser().parse_args(argv)\n"
            "    if args.version:\n"
            "        print('taskboard 0.1.0')\n"
            "    else:\n"
            "        print('TaskBoard scaffold')\n"
            "    return 0\n"
        ),
        "src/taskboard/analytics.py": "def summarize(tasks):\n    return {'total': len(tasks)}\n",
        "src/taskboard/filters.py": "def by_status(tasks, status):\n    return [t for t in tasks if getattr(t, 'status', None) == status]\n",
        "src/taskboard/formatters.py": "def as_table(tasks):\n    return '\\n'.join(str(t) for t in tasks)\n",
        "src/taskboard/importers.py": "def import_lines(text):\n    return [line.strip() for line in text.splitlines() if line.strip()]\n",
        "src/taskboard/exporters.py": "def export_markdown(tasks):\n    return '\\n'.join(f'- {t}' for t in tasks)\n",
        "src/taskboard/validation.py": "def validate_status(status):\n    return status in {'todo', 'doing', 'done', 'blocked'}\n",
        "src/taskboard/config.py": "DEFAULT_STATUSES = ['todo', 'doing', 'done', 'blocked']\n",
        "src/taskboard/errors.py": "class TaskBoardError(Exception):\n    pass\n",
        "src/taskboard/sample_data.py": "SAMPLE_TASKS = ['write docs', 'add tests', 'ship cli']\n",
        "tests/__init__.py": "",
        "tests/test_cli.py": (
            "from taskboard.cli import main\n\n"
            "def test_main_version(capsys):\n"
            "    assert main(['--version']) == 0\n"
            "    assert 'taskboard' in capsys.readouterr().out.lower()\n"
        ),
        "tests/test_models.py": "from taskboard.models import Task\n\ndef test_task_defaults():\n    assert Task('x').status == 'todo'\n",
        "tests/test_storage.py": "def test_storage_placeholder():\n    assert True\n",
        "tests/test_filters.py": "def test_filters_placeholder():\n    assert True\n",
        "tests/test_analytics.py": "def test_analytics_placeholder():\n    assert True\n",
        "docs/overview.md": "# Overview\n\nTaskBoard is a local planning tool.\n",
        "docs/cli.md": "# CLI\n\nDocument commands here.\n",
        "docs/data-format.md": "# Data Format\n\nDocument the JSON schema here.\n",
        "docs/maintenance-log.md": "# Maintenance Log\n\nSupervisor notes will be reflected here.\n",
        "data/samples/tasks.json": "[{\"title\":\"draft roadmap\",\"status\":\"todo\",\"priority\":2}]\n",
        "data/samples/import.txt": "write parser\nadd export command\nreview docs\n",
        "data/fixtures/empty.json": "[]\n",
        "config/statuses.json": "[\"todo\", \"doing\", \"done\", \"blocked\"]\n",
        "scripts/check_project.py": (
            "from pathlib import Path\n"
            "required = ['src/taskboard/cli.py', 'src/taskboard/storage.py', 'README.md']\n"
            "missing = [p for p in required if not Path(p).exists()]\n"
            "if missing:\n"
            "    raise SystemExit('missing: ' + ', '.join(missing))\n"
            "print('project structure ok')\n"
        ),
        "scripts/dev_notes.md": "# Dev Notes\n\nUse small, verified changes.\n",
        "examples/basic_usage.md": "# Basic Usage\n\nExamples will be added by the agent.\n",
        "examples/sample_export.md": "# Sample Export\n\nNo export yet.\n",
    }
    for rel, text in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return project


@dataclass
class ProjectState:
    project_id: int
    path: Path
    stage: int = 0
    turn: int = 0
    completed: bool = False
    last_result: dict[str, Any] = field(default_factory=dict)
    last_validation: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return detect_project_kind(self.path)

PROJECT_STAGE_GOALS: dict[str, list[dict[str, Any]]] = {
    "taskboard": [
        {
            "name": "core_cli",
            "request": (
                "Implement the first useful TaskBoard CLI slice across this existing multi-file project. "
                "Add commands to create/list/update tasks, keep the library modules coherent, preserve the directory structure, "
                "and run at least `python -m compileall src tests` plus a CLI smoke command with `PYTHONPATH=src`. "
                "Use environment tools to copy files into the workspace, edit them, and apply replacements back to the project."
            ),
            "required": ["argparse", "add", "list", "update", "json", "compileall"],
        },
        {
            "name": "persistence_reports",
            "request": (
                "The core CLI passed. Add durable JSON persistence, filtering by status/tag, summary reporting, and markdown export. "
                "Update docs/data-format.md and docs/cli.md so the commands and data shape match the implementation. "
                "Run compile checks and at least one `PYTHONPATH=src` command that writes and reads a temporary task database."
            ),
            "required": ["load_tasks", "save_tasks", "summary", "markdown", "tag", "status"],
        },
        {
            "name": "tests_docs",
            "request": (
                "Improve engineering quality. Expand the tests so CLI parsing, persistence, filters, analytics, import, and export behavior are covered. "
                "Make scripts/check_project.py verify important files and runnable commands. Update README with install, usage, and maintenance notes. "
                "Run the project check script and compile checks."
            ),
            "required": ["pytest", "test_cli", "test_storage", "check_project", "usage", "maintenance"],
        },
        {
            "name": "hardening",
            "request": (
                "Do a final hardening pass. Improve error messages, validation, backup awareness, sample data, and docs consistency. "
                "Avoid rewriting unrelated files. Verify the package can be imported and executed with PYTHONPATH=src, run compile checks, "
                "and leave a concise maintenance entry in docs/maintenance-log.md."
            ),
            "required": ["error", "validate", "backup", "sample", "maintenance-log", "PYTHONPATH"],
        },
    ],
    "snake": [
        {
            "name": "playable_core",
            "request": (
                "Turn this Snake Arcade scaffold into a runnable terminal game. Implement board state, snake movement, food placement, collision/game-over logic, "
                "score tracking, a CLI entrypoint with `--demo` or equivalent non-interactive smoke mode, and clear separation between game logic and rendering. "
                "Run `python -m compileall src tests` and a `PYTHONPATH=src` smoke command."
            ),
            "required": ["argparse", "snake", "food", "collision", "score", "compileall"],
        },
        {
            "name": "modes_persistence",
            "request": (
                "The playable core passed. Add high-score persistence, configurable board size/tick rate, and at least one extra mode such as walls, timed, or wrap. "
                "Update docs/controls.md and docs/modes.md so they match the implementation, and run a command that exercises the new mode without interactive input."
            ),
            "required": ["load_scores", "save_scores", "mode", "config", "controls", "score"],
        },
        {
            "name": "tests_docs",
            "request": (
                "Expand engineering coverage. Add tests for movement, food spawning, collision, scoring, rendering, CLI parsing, and persistence. "
                "Make scripts/check_project.py run meaningful import/smoke checks. Update README with install, run, test, and maintenance notes."
            ),
            "required": ["pytest", "test_game", "test_render", "check_project", "usage", "maintenance"],
        },
        {
            "name": "hardening",
            "request": (
                "Do a final hardening pass for Snake Arcade. Improve validation and error messages, add deterministic demo/sample data, document recovery/backup expectations, "
                "run compile and smoke checks, and leave a concise maintenance entry in docs/maintenance-log.md."
            ),
            "required": ["error", "validate", "backup", "sample", "maintenance-log", "PYTHONPATH"],
        },
    ],
    "dataset": [
        {
            "name": "pipeline_core",
            "request": (
                "Turn this DatasetOps scaffold into a working dataset CLI. Implement CSV loading, schema validation, row normalization, summary statistics, "
                "and a command that validates data/raw/sample.csv. Keep loader/schema/validate/summary modules coherent and run compile plus a PYTHONPATH=src smoke command."
            ),
            "required": ["argparse", "load_rows", "validate_rows", "csv", "summary", "compileall"],
        },
        {
            "name": "reports_exports",
            "request": (
                "The core pipeline passed. Add markdown/JSON report export, invalid-row reporting, configurable input/output paths, and processed output writing. "
                "Update docs/schema.md, docs/pipeline.md, and docs/reports.md so they match the implementation."
            ),
            "required": ["export", "markdown", "json", "invalid", "config", "processed"],
        },
        {
            "name": "tests_docs",
            "request": (
                "Improve engineering quality. Add tests for loader, schema validation, bad rows, summary, export, and CLI parsing. "
                "Make scripts/check_project.py run meaningful import/smoke checks. Update README with install, usage, data contract, and maintenance notes."
            ),
            "required": ["pytest", "test_validate", "test_summary", "check_project", "usage", "maintenance"],
        },
        {
            "name": "hardening",
            "request": (
                "Do a final hardening pass for DatasetOps. Improve error messages, validation coverage, sample data, backup/recovery notes, and docs consistency. "
                "Run compile and smoke checks, then leave a concise maintenance entry in docs/maintenance-log.md."
            ),
            "required": ["error", "validate", "backup", "sample", "maintenance-log", "PYTHONPATH"],
        },
    ],
    "multilang": [
        {
            "name": "build_probe",
            "request": (
                "Map this mixed-language PolyBench repository and make the verification loop real. "
                "Improve scripts/check_project.py and docs/build.md as needed so it can compile the C sort demo, "
                "compile the C++ graph demo, run the JS syntax/runtime smoke check when tools are available, and run Python import/compile checks. "
                "Use `python scripts/check_project.py` as the single acceptance command; run direct compiler commands only to diagnose a failure, "
                "then fold the fix back into scripts/check_project.py. The script should visibly include either compileall or py_compile for Python checks. "
                "Use environment tools for file edits, keep the tree structure, and finish by rerunning that script."
            ),
            "required": ["check_project", "gcc", "g++", "node", "compileall", "build"],
        },
        {
            "name": "cross_language_features",
            "request": (
                "The build probe passed. Add cross-language functionality without adding external dependencies: "
                "Python should load numeric fixtures and produce a report, C should expose sorted/min/max behavior, "
                "C++ should expose graph edge aggregation, and JS utilities should format report rows. "
                "Update docs/architecture.md, docs/native.md, and docs/js-utils.md to match the implementation, then rerun `python scripts/check_project.py` as the acceptance gate."
            ),
            "required": ["load_numbers", "report", "sorted", "min", "max", "total_weight", "formatMetric"],
        },
        {
            "name": "tests_regression",
            "request": (
                "Add regression coverage across the project. Expand Python tests for datasets/metrics/reports/CLI, "
                "add native and graph smoke coverage to scripts/check_project.py, add at least one JS table/format check, "
                "and make failure messages actionable. Rerun compile/import/build checks."
            ),
            "required": ["pytest", "test_metrics", "test_datasets", "test_reports", "sortbench", "graph_demo", "js"],
        },
        {
            "name": "hardening_docs",
            "request": (
                "Do a final hardening pass. Make the repository maintainable for a future engineer: document toolchain detection, "
                "build artifacts, data contracts, and recovery/backup expectations; keep generated outputs under build/ or reports/; "
                "run `python scripts/check_project.py` and leave a concise entry in docs/maintenance-log.md."
            ),
            "required": ["toolchain", "artifact", "data contract", "backup", "maintenance-log", "check_project"],
        },
    ],
}

# Backward-compatible name used by older ad-hoc validators/tests.
STAGE_GOALS = PROJECT_STAGE_GOALS["taskboard"]


def detect_project_kind(project: Path) -> str:
    name = project.name.lower()
    if "polybench" in name or (project / "native" / "include" / "sortlib.h").exists():
        return "multilang"
    if "snake" in name or (project / "src" / "snake_arcade").exists():
        return "snake"
    if "dataset" in name or (project / "src" / "datasetops").exists():
        return "dataset"
    return "taskboard"


def stage_goals_for_project(project: Path | str) -> list[dict[str, Any]]:
    kind = project if isinstance(project, str) else detect_project_kind(project)
    return PROJECT_STAGE_GOALS.get(kind, PROJECT_STAGE_GOALS["taskboard"])


PROJECT_LAYOUTS = {
    "taskboard": {
        "module": "taskboard",
        "dirs": ["src/taskboard", "tests", "docs", "data", "scripts", "config", "examples"],
        "core_paths": [
            "README.md", "pyproject.toml", "src/taskboard/cli.py", "src/taskboard/models.py",
            "src/taskboard/storage.py", "src/taskboard/analytics.py", "tests/test_cli.py",
            "docs/cli.md", "scripts/check_project.py",
        ],
    },
    "snake": {
        "module": "snake_arcade",
        "dirs": ["src/snake_arcade", "tests", "docs", "assets", "scripts", "config", "examples"],
        "core_paths": [
            "README.md", "pyproject.toml", "src/snake_arcade/cli.py", "src/snake_arcade/game.py",
            "src/snake_arcade/render.py", "tests/test_cli.py", "docs/controls.md",
            "docs/modes.md", "scripts/check_project.py",
        ],
    },
    "dataset": {
        "module": "datasetops",
        "dirs": ["src/datasetops", "tests", "docs", "data", "reports", "scripts", "config"],
        "core_paths": [
            "README.md", "pyproject.toml", "src/datasetops/cli.py", "src/datasetops/loader.py",
            "src/datasetops/validate.py", "src/datasetops/summary.py", "tests/test_cli.py",
            "docs/schema.md", "scripts/check_project.py",
        ],
    },
    "multilang": {
        "module": "polybench",
        "dirs": ["src/polybench", "tests", "docs", "data", "scripts", "config", "native", "cpp", "js", "reports"],
        "core_paths": [
            "README.md", "pyproject.toml", "Makefile", "src/polybench/cli.py",
            "src/polybench/datasets.py", "native/include/sortlib.h", "native/src/sortlib.c",
            "native/src/sortbench.c", "cpp/include/graph.hpp", "cpp/src/graph.cpp",
            "js/src/format.js", "scripts/check_project.py", "docs/build.md",
        ],
    },
}


def validate_project(project: Path, stage: int) -> dict[str, Any]:
    kind = detect_project_kind(project)
    goals = stage_goals_for_project(kind)
    layout = PROJECT_LAYOUTS[kind]
    files = {
        str(p.relative_to(project)).replace("\\", "/"): p
        for p in project.rglob("*")
        if p.is_file()
        and "_env" not in p.parts
        and "__pycache__" not in p.parts
        and ".pytest_cache" not in p.parts
        and ".write_backups" not in p.parts
    }
    text_by_name = {
        name: path.read_text(encoding="utf-8", errors="ignore")
        for name, path in files.items()
        if path.suffix.lower() in {".py", ".md", ".json", ".toml", ".txt", ".c", ".h", ".hpp", ".cpp", ".js", ".csv"}
        or path.name.lower() == "makefile"
    }
    all_text = "\n".join(text_by_name.values()).lower()
    current_goal = goals[min(stage, len(goals) - 1)]

    def _has_token(key: str) -> bool:
        if key == "test_cli":
            cli_test = text_by_name.get("tests/test_cli.py", "").lower()
            return (
                "def test_" in cli_test
                and ("cli" in cli_test or "main(" in cli_test or "build_parser" in cli_test)
            )
        if key == "test_storage":
            storage_test = text_by_name.get("tests/test_storage.py", "").lower()
            return "def test_" in storage_test and ("storage" in storage_test or "load_tasks" in storage_test)
        if key == "test_game":
            return "def test_" in text_by_name.get("tests/test_game.py", "").lower()
        if key == "test_render":
            return "def test_" in text_by_name.get("tests/test_render.py", "").lower()
        if key == "test_validate":
            return "def test_" in text_by_name.get("tests/test_validate.py", "").lower()
        if key == "test_summary":
            return "def test_" in text_by_name.get("tests/test_summary.py", "").lower()
        if key == "test_metrics":
            text = text_by_name.get("tests/test_metrics.py", "").lower()
            return "def test_" in text and ("mean" in text or "metric" in text)
        if key == "test_datasets":
            text = text_by_name.get("tests/test_datasets.py", "").lower()
            return "def test_" in text and ("load_numbers" in text or "dataset" in text)
        if key == "test_reports":
            text = text_by_name.get("tests/test_reports.py", "").lower()
            return "def test_" in text and ("render_summary" in text or "report" in text)
        if key == "check_project":
            check_script = text_by_name.get("scripts/check_project.py", "").lower()
            return bool(check_script.strip()) and (layout["module"] in check_script or "check" in check_script)
        aliases = {
            "argparse": ["argparse"],
            "add": ["add", "create"],
            "list": ["list", "show"],
            "update": ["update", "set-status", "complete"],
            "json": ["json"],
            "load_tasks": ["load_tasks", "load tasks"],
            "save_tasks": ["save_tasks", "save tasks"],
            "summary": ["summary", "summarize", "analytics"],
            "markdown": ["markdown", "export_markdown"],
            "tag": ["tag", "tags"],
            "status": ["status"],
            "pytest": ["pytest"],
            "test_cli": ["test_cli", "cli parsing"],
            "test_storage": ["test_storage", "storage"],
            "check_project": ["check_project", "project structure ok"],
            "usage": ["usage", "commands"],
            "maintenance": ["maintenance", "maintain"],
            "error": ["error", "exception", "invalid"],
            "validate": ["validate", "validation"],
            "backup": ["backup", ".backups", "backups"],
            "sample": ["sample", "fixture"],
            "maintenance-log": ["maintenance-log", "maintenance log"],
            "PYTHONPATH": ["pythonpath", "pythonpath=src"],
            "gcc": ["gcc", "sortbench", "-std=c11"],
            "g++": ["g++", "graph_demo", "-std=c++17"],
            "node": ["node", "format.js", "js"],
            "build": ["build", "toolchain", "compile"],
            "load_numbers": ["load_numbers"],
            "report": ["report", "summary.md", "render_summary"],
            "sorted": ["sorted", "sort", "is_sorted_ints"],
            "min": ["min", "minimum"],
            "max": ["max", "maximum"],
            "total_weight": ["total_weight"],
            "formatMetric": ["formatmetric", "formatmetric", "format metric"],
            "test_reports": ["test_reports", "render_summary", "report"],
            "sortbench": ["sortbench"],
            "graph_demo": ["graph_demo"],
            "js": ["javascript", "format.js", "node"],
            "toolchain": ["toolchain", "gcc", "g++", "node"],
            "artifact": ["artifact", "build/", "reports/"],
            "data contract": ["data contract", "whitespace-separated", "fixture"],
            "snake": ["snake", "snakegame", "snake_game"],
            "food": ["food"],
            "collision": ["collision", "game over", "game_over"],
            "score": ["score", "scores"],
            "load_scores": ["load_scores", "load score"],
            "save_scores": ["save_scores", "save score"],
            "mode": ["mode", "classic", "timed", "wrap", "wall"],
            "config": ["config", "configuration"],
            "controls": ["controls", "keyboard", "key"],
            "load_rows": ["load_rows", "csv.dictreader", "read_csv"],
            "validate_rows": ["validate_rows", "validate row"],
            "csv": ["csv"],
            "export": ["export"],
            "invalid": ["invalid", "bad row", "bad_rows"],
            "processed": ["processed", "write_json"],
        }
        return any(alias.lower() in all_text for alias in aliases.get(key, [key]))

    required_hits = {key: _has_token(key) for key in current_goal["required"]}
    core_paths = layout["core_paths"]
    dir_hits = {
        rel: any(path == rel or path.startswith(rel + "/") for path in files)
        for rel in layout["dirs"]
    }
    compile_result: dict[str, Any] = {"attempted": False}
    subprocess_env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    mingw_bin = PROJECT_ROOT / "mingw64" / "bin"
    if mingw_bin.exists():
        subprocess_env["PATH"] = str(mingw_bin) + os.pathsep + subprocess_env.get("PATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "src", "tests"],
            cwd=str(project),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=subprocess_env,
            timeout=20,
        )
        compile_result = {
            "attempted": True,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-500:],
            "stderr": proc.stderr[-500:],
        }
    except Exception as e:
        compile_result = {"attempted": True, "error": f"{type(e).__name__}: {e}"}

    if "compileall" in required_hits:
        required_hits["compileall"] = compile_result.get("returncode") == 0

    import_result: dict[str, Any] = {"attempted": False}
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys; sys.path.insert(0,'src'); import {layout['module']} as m; print(getattr(m,'__version__','ok'))",
            ],
            cwd=str(project),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=subprocess_env,
            timeout=10,
        )
        import_result = {
            "attempted": True,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-500:],
            "stderr": proc.stderr[-500:],
        }
    except Exception as e:
        import_result = {"attempted": True, "error": f"{type(e).__name__}: {e}"}

    check_script_text = text_by_name.get("scripts/check_project.py", "")
    multilang_check_ready = (
        kind == "multilang"
        and ("compileall" in check_script_text or "py_compile" in check_script_text or "compile(" in check_script_text)
        and ("gcc" in check_script_text or "sortbench" in check_script_text)
        and ("g++" in check_script_text or "graph_demo" in check_script_text)
        and ("node" in check_script_text or "js/src" in check_script_text)
    )
    project_check_result: dict[str, Any] = {"attempted": False}
    if kind == "multilang" and multilang_check_ready and (project / "scripts" / "check_project.py").exists():
        try:
            env = {**subprocess_env, "PYTHONPATH": str(project / "src")}
            proc = subprocess.run(
                [sys.executable, "scripts/check_project.py"],
                cwd=str(project),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=90,
            )
            project_check_result = {
                "attempted": True,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-2000:],
            }
        except Exception as e:
            project_check_result = {"attempted": True, "error": f"{type(e).__name__}: {e}"}

    def _multilang_check_accepts_environment_skips(result: dict[str, Any]) -> bool:
        if not result.get("attempted"):
            return False
        stdout = str(result.get("stdout") or "").lower()
        stderr = str(result.get("stderr") or "").lower()
        if result.get("returncode") == 0:
            return True
        if result.get("error"):
            return False
        if "traceback" in stdout or "traceback" in stderr:
            return False
        blocking_failures = []
        for line in stdout.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if " fail" not in lower and "fail:" not in lower and "failures:" not in lower:
                continue
            if "permission denied" in lower or "not installed" in lower or "no makefile" in lower:
                continue
            if lower.startswith("check_project: failures:"):
                names = [
                    part.strip()
                    for part in lower.split(":", 2)[-1].replace(";", ",").split(",")
                    if part.strip()
                ]
                unresolved = [
                    name for name in names
                    if name not in {"node", "build", "make"}
                ]
                if unresolved:
                    blocking_failures.append(stripped)
                continue
            blocking_failures.append(stripped)
        if blocking_failures:
            result["blocking_failures"] = blocking_failures[:5]
            return False
        required_signals = ("gcc", "g++", "node", "python")
        return all(signal in stdout for signal in required_signals)

    backup_count = sum(1 for p in project.rglob(".env_backups/*") if p.is_file())
    checks = {
        "file_count": len(files),
        "has_complex_tree": len(files) >= 30 and all(dir_hits.values()),
        "dir_hits": dir_hits,
        "core_paths": {rel: rel in files for rel in core_paths},
            "required_hits": required_hits,
            "multilang_check_ready": multilang_check_ready if kind == "multilang" else None,
            "backup_count": backup_count,
        }
    if kind == "multilang":
        check_script_ok = _multilang_check_accepts_environment_skips(project_check_result)
        if "check_project" in required_hits:
            required_hits["check_project"] = required_hits["check_project"] and multilang_check_ready and check_script_ok
        for key in ("gcc", "g++", "node", "build", "sortbench", "graph_demo", "js"):
            if key in required_hits:
                required_hits[key] = required_hits[key] and check_script_ok
    static_ok = (
        checks["has_complex_tree"]
        and all(checks["core_paths"].values())
        and all(required_hits.values())
        and compile_result.get("returncode") == 0
        and import_result.get("returncode") == 0
        and (kind != "multilang" or (multilang_check_ready and _multilang_check_accepts_environment_skips(project_check_result)))
    )
    return {
        "kind": kind,
        "stage": stage,
        "stage_name": current_goal["name"],
        "ok": static_ok,
        "checks": checks,
        "compile_result": compile_result,
        "import_result": import_result,
        "project_check_result": project_check_result,
        "files": sorted(files),
    }


def next_message(state: ProjectState) -> str:
    goals = stage_goals_for_project(state.path)
    validation = state.last_validation or validate_project(state.path, state.stage)
    goal = goals[min(state.stage, len(goals) - 1)]
    if state.turn == 0:
        return goal["request"]
    if not validation.get("ok"):
        missing = [
            key for key, hit in (validation.get("checks", {}).get("required_hits") or {}).items()
            if not hit
        ]
        return (
            f"Stage {state.stage + 1} `{goal['name']}` for `{state.kind}` did not pass review. "
            f"Missing or unclear acceptance targets: {', '.join(missing) or 'base files or load links'}. "
            "This is an actionable patch request, not a status question. Read the existing files first, patch only the gaps, "
            "and make the exact missing target names visible in source/docs/tests where appropriate. "
            "Then verify with grep/search plus the project-specific smoke/static checks before replying."
        )
    if state.stage + 1 < len(goals):
        next_goal = goals[state.stage + 1]
        return (
            f"Stage {state.stage + 1} `{goal['name']}` for `{state.kind}` passed review. "
            f"Now move to `{next_goal['name']}`.\n{next_goal['request']}"
        )
    return (
        "The final stage basically passed. Do a closing review: check README, core source, and the verification script for consistency, "
        "run the verification command, and either fix small issues or provide the final maintenance report."
    )


def unfinished_tasks(tasks) -> list:
    return [task for task in tasks if not task.done()]


async def monitor_loop(client: EnvironmentClient, recorder: EnvRecorder, stop: asyncio.Event, interval: float) -> None:
    while not stop.is_set():
        snap: dict[str, Any] = {"kind": "monitor"}
        try:
            snap["active"] = await client.active()
        except Exception as e:
            snap["active_error"] = f"{type(e).__name__}: {e}"
        try:
            snap["active_commands"] = await client.active_commands()
        except Exception as e:
            snap["active_commands_error"] = f"{type(e).__name__}: {e}"
        try:
            snap["monitor_events"] = await client.monitor_sample(duration_sec=2.5)
        except Exception as e:
            snap["monitor_sample_error"] = f"{type(e).__name__}: {e}"
        await recorder.record(snap)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run(args: argparse.Namespace) -> Path:
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    run_dir = RUNS_DIR / run_id
    recorder = EnvRecorder(run_dir)
    projects_root = run_dir / "projects"
    selected_kinds = tuple(k.strip() for k in (args.project_kinds or "").split(",") if k.strip()) or None
    projects = [ProjectState(i, create_project(projects_root, i, selected_kinds)) for i in range(args.projects)]
    client = EnvironmentClient(args.base_url, recorder)
    await client.health()

    await recorder.record({
        "kind": "run_start",
        "run_id": run_id,
        "base_url": args.base_url,
        "duration_min": args.duration_min,
        "projects": [str(p.path) for p in projects],
    })
    stop = asyncio.Event()
    monitor_task = asyncio.create_task(monitor_loop(client, recorder, stop, args.monitor_interval_sec))
    end_at = time.monotonic() + args.duration_min * 60
    async def project_loop(state: ProjectState) -> None:
        user_id = f"env_pm_{state.project_id}"
        while time.monotonic() < end_at and not state.completed:
            state.last_validation = validate_project(state.path, state.stage)
            message = next_message(state)
            await recorder.record({
                "kind": "supervisor_request",
                "project": str(state.path),
                "stage": state.stage,
                "turn": state.turn,
                "validation": state.last_validation,
                "message": message,
            })
            try:
                result = await client.ask_environment(
                    user_id=user_id,
                    current_dir=state.path,
                    message=message,
                    client_msg_id=f"{run_id}_p{state.project_id}_t{state.turn}_{uuid.uuid4().hex[:8]}",
                )
                state.last_result = result
                after = validate_project(state.path, state.stage)
                state.last_validation = after
                await recorder.record({
                    "kind": "environment_call",
                    "project": str(state.path),
                    "stage": state.stage,
                    "turn": state.turn,
                    "message": message,
                    "validation_after": after,
                    **result,
                })
                if result.get("ok") and after.get("ok"):
                    state.stage += 1
                    if state.stage >= len(stage_goals_for_project(state.path)):
                        state.completed = True
                state.turn += 1
            except asyncio.CancelledError:
                after = validate_project(state.path, state.stage)
                await recorder.record({
                    "kind": "environment_call",
                    "project": str(state.path),
                    "stage": state.stage,
                    "turn": state.turn,
                    "message": message,
                    "validation_after": after,
                    "ok": False,
                    "status_code": 0,
                    "cancelled": True,
                    "error": "cancelled while waiting for environment result",
                })
                raise
            await asyncio.sleep(max(1.0, args.review_gap_sec))

    pending_projects = list(projects)
    tasks: dict[asyncio.Task, ProjectState] = {}
    max_inflight = max(1, args.max_inflight)

    def _fill_project_slots() -> None:
        while len(tasks) < max_inflight and pending_projects and time.monotonic() < end_at:
            state = pending_projects.pop(0)
            task = asyncio.create_task(project_loop(state))
            tasks[task] = state

    _fill_project_slots()
    try:
        while time.monotonic() < end_at and tasks:
            done, _ = await asyncio.wait(
                set(tasks),
                timeout=max(0.1, min(5.0, end_at - time.monotonic())),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                tasks.pop(task, None)
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    await recorder.record({"kind": "project_loop_error", "error": f"{type(e).__name__}: {e}"})
            _fill_project_slots()
    finally:
        await recorder.record({"kind": "run_stopping", "inflight": len(tasks)})
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=args.drain_sec)
            except asyncio.TimeoutError:
                await asyncio.sleep(0)
                remaining = unfinished_tasks(tasks)
                if remaining:
                    await recorder.record({"kind": "drain_timeout", "remaining": len(remaining)})
                for task in remaining:
                    task.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)
        stop.set()
        await monitor_task
        await client.close()
        await write_summary(run_dir, projects)
    return run_dir


async def write_summary(run_dir: Path, projects: list[ProjectState]) -> None:
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    calls = [e for e in events if e.get("kind") == "environment_call"]
    lat = [float(e.get("latency_sec") or 0) for e in calls if e.get("latency_sec")]
    workflow_count = sum(len(e.get("workflow") or []) for e in calls)
    command_count = sum(len(e.get("command_events") or []) for e in calls)
    monitor_events = sum(len(e.get("monitor_events") or []) for e in events if e.get("kind") == "monitor")
    def _call_validation_ok(event: dict[str, Any]) -> bool:
        validation = event.get("validation_after")
        return isinstance(validation, dict) and validation.get("ok") is True

    def _call_failed(event: dict[str, Any]) -> bool:
        return (
            event.get("ok") is False
            and not (event.get("cancelled") and _call_validation_ok(event))
        ) or (
            isinstance(event.get("validation_after"), dict)
            and event["validation_after"].get("ok") is False
        )

    ineffective = [
        e for e in calls
        if ineffective_reply_reasons(str(e.get("text") or ""))
        or (isinstance(e.get("validation_after"), dict) and e["validation_after"].get("ok") is False)
    ]
    errors = [
        e for e in events
        if (e.get("kind") == "environment_call" and _call_failed(e))
        or (e.get("kind") != "environment_call" and e.get("ok") is False)
        or e.get("errors")
        or ("error" in e and not (e.get("cancelled") and _call_validation_ok(e)))
        or e in ineffective
    ]
    summary = {
        "events": len(events),
        "environment_calls": len(calls),
        "success": sum(1 for e in calls if is_effective_success(e)),
        "fail": sum(1 for e in calls if _call_failed(e)),
        "ineffective": len(ineffective),
        "workflow_events": workflow_count,
        "command_events": command_count,
        "monitor_events": monitor_events,
        "errors": len(errors),
        "latency": {
            "count": len(lat),
            "min": min(lat) if lat else None,
            "max": max(lat) if lat else None,
            "mean": statistics.mean(lat) if lat else None,
            "median": statistics.median(lat) if lat else None,
        },
        "projects": [
            {
                "project": str(p.path),
                "kind": p.kind,
                "stage": p.stage,
                "turn": p.turn,
                "completed": p.completed,
                "validation": validate_project(
                    p.path,
                    min(p.stage, len(stage_goals_for_project(p.path)) - 1),
                ),
            }
            for p in projects
        ],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Environment Maintenance Stress Report",
        "",
        f"- Events: {summary['events']}",
        f"- Calls: {summary['environment_calls']}",
        f"- Success/fail/ineffective: {summary['success']}/{summary['fail']}/{summary['ineffective']}",
        f"- Workflow events: {summary['workflow_events']}",
        f"- Command events: {summary['command_events']}",
        f"- Monitor events: {summary['monitor_events']}",
        f"- Error-like events: {summary['errors']}",
    ]
    if lat:
        lines.append(
            f"- Latency sec: min {summary['latency']['min']:.1f}, "
            f"median {summary['latency']['median']:.1f}, "
            f"mean {summary['latency']['mean']:.1f}, max {summary['latency']['max']:.1f}"
        )
    lines.extend(["", "## Errors", ""])
    for e in errors[:30]:
        q = ineffective_reply_reasons(str(e.get("text") or ""))
        suffix = f" quality={q}" if q else ""
        lines.append(f"- `{e.get('ts')}` {e.get('kind')}: {str(e.get('error') or e.get('errors') or e)[:700]}{suffix}")
    lines.extend(["", "## Project Progress", ""])
    for p in summary["projects"]:
        lines.append(
            f"- `{p['project']}` stage={p['stage']} turns={p['turn']} "
            f"completed={p['completed']} ok={p['validation'].get('ok')}"
        )
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8017")
    p.add_argument("--duration-min", type=float, default=60.0)
    p.add_argument("--projects", type=int, default=4)
    p.add_argument("--project-kinds", default="", help="Comma-separated project kinds: taskboard,snake,dataset,multilang")
    p.add_argument("--max-inflight", type=int, default=2)
    p.add_argument("--review-gap-sec", type=float, default=20.0)
    p.add_argument("--monitor-interval-sec", type=float, default=15.0)
    p.add_argument("--drain-sec", type=float, default=900.0)
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
