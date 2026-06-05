"""Export a sanitized public snapshot of this repo for open-sourcing.

Whitelist-based: only paths explicitly listed below are copied. The script
never modifies the source tree. After copying, a secret-pattern scanner
re-checks the snapshot and aborts if anything suspicious is found.

Usage (PowerShell)::

    .venv/Scripts/python.exe scripts/export_public_snapshot.py [--out F:/chatbot-public] [--dry-run] [--force]

After a successful run, cd into the output dir and initialize git manually::

    cd F:/chatbot-public
    git init
    git add -A
    git commit -m "Initial public snapshot"
    git remote add origin <your-github-url>
    git branch -M main
    git push -u origin main
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── Whitelist ──────────────────────────────────────────────────────────
# Directories copied recursively. Each entry is a path relative to ROOT.
INCLUDE_DIRS: list[str] = [
    "app",
    "tests",
    "migrations",
    "scripts",
    "config",
    "agent_frontend",
]

# Files copied at the repo root.
INCLUDE_FILES: list[str] = [
    "README.md",
    "LICENSE",
    ".env.example",
    ".gitignore",
    "pyproject.toml",
    "pytest.ini",
    "conftest.py",
    "Makefile",
    "docker-compose.yml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-optional.txt",
    "napcat_bridge.py",
    "ocr_bridge.py",
    "log_viewer_server.py",
    "botctl_helper.py",
    "start.bat",
    "start_backend.bat",
    "start_agent.bat",
    "start_qqbot.bat",
    "startbot.bat",
    "stop_all_services.bat",
    "stop_all_services.ps1",
    "switch_model_pool.bat",
    "open_agent.bat",
    "botctl.bat",
    "cleanup_generated.bat",
    "monitor.sh",
]

# Top-level scripts under stress_tools/, but not stress_tools/runs/.
INCLUDE_STRESS_TOOLS_TOP_LEVEL = True

# Specific docs allowed (whitelist inside docs/).
INCLUDE_DOCS: list[str] = [
    "app_refactor_inventory.md",
    "environment_mode_plan.md",
    "file_management_legacy_replacement_map.md",
    "file_management_refactor_plan.md",
]

# Personas allowed (only environment).
INCLUDE_PERSONAS: list[str] = ["environment.md"]

# ── Per-tree exclusions ────────────────────────────────────────────────
EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".benchmarks",
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".git",
    ".idea",
    ".vscode",
    "runs",  # excludes stress_tools/runs/
    "debug_logs",
    "logs",
    "dist",
    "build",
    ".egg-info",
}

EXCLUDE_FILE_GLOBS = [
    "*.pyc",
    "*.pyo",
    "*.log",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.sqlite",
    "*.sqlite3",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.7z",
    "*.wav",
    "*.mp3",
    "*.mp4",
    ".env",
    ".env.local",
    ".env.*.local",
]

# ── Secret scanner ─────────────────────────────────────────────────────
# Matches things that look like real keys/tokens. The placeholder
# `sk-your-deepseek-key-here` is intentionally not matchable.
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{32,}"),  # OpenAI/DeepSeek-style
    re.compile(r"DEEPSEEK_API_KEY\s*=\s*sk-[A-Za-z0-9]+"),
    re.compile(r"GPT55_API_KEY\s*=\s*sk-[A-Za-z0-9]+"),
    re.compile(r"chat\.ekti\.cc"),  # private proxy
    # private QQ accounts (long pure-digit ids); allow short test fixtures.
    re.compile(r"\bqq[_=]\s*[\"']?\d{8,}\b", re.IGNORECASE),
]

# Files where pattern matches are expected and should be ignored.
SECRET_SCAN_ALLOWLIST = {
    "scripts/export_public_snapshot.py",  # this file lists patterns literally
    "tests/test_message_routing.py",  # uses 1234567890 as a fake QQ fixture
}

TEXT_EXTS = {
    ".py",
    ".md",
    ".txt",
    ".cfg",
    ".ini",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".bat",
    ".ps1",
    ".sh",
    ".env",
    ".example",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".sql",
    ".dockerfile",
}


def _is_excluded_dir(name: str) -> bool:
    return name in EXCLUDE_DIR_NAMES or name.endswith(".egg-info")


def _is_excluded_file(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_FILE_GLOBS)


def _copy_tree(src: Path, dst: Path, plan: list[tuple[Path, Path]]) -> None:
    """Walk src and add (file_src, file_dst) entries to plan, honoring exclusions."""
    if not src.exists():
        return
    if src.is_file():
        if not _is_excluded_file(src.name):
            plan.append((src, dst))
        return
    for entry in src.iterdir():
        if entry.is_dir():
            if _is_excluded_dir(entry.name):
                continue
            _copy_tree(entry, dst / entry.name, plan)
        else:
            if _is_excluded_file(entry.name):
                continue
            plan.append((entry, dst / entry.name))


def build_plan(out_dir: Path) -> list[tuple[Path, Path]]:
    plan: list[tuple[Path, Path]] = []

    for d in INCLUDE_DIRS:
        _copy_tree(ROOT / d, out_dir / d, plan)

    for f in INCLUDE_FILES:
        src = ROOT / f
        if src.exists():
            plan.append((src, out_dir / f))

    if INCLUDE_STRESS_TOOLS_TOP_LEVEL:
        st_src = ROOT / "stress_tools"
        if st_src.exists():
            for entry in st_src.iterdir():
                if entry.is_file() and not _is_excluded_file(entry.name):
                    plan.append((entry, out_dir / "stress_tools" / entry.name))

    docs_src = ROOT / "docs"
    for name in INCLUDE_DOCS:
        src = docs_src / name
        if src.exists():
            plan.append((src, out_dir / "docs" / name))

    personas_src = ROOT / "personas"
    for name in INCLUDE_PERSONAS:
        src = personas_src / name
        if src.exists():
            plan.append((src, out_dir / "personas" / name))

    return plan


def perform_copy(plan: list[tuple[Path, Path]]) -> None:
    for src, dst in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


# Files where private/proxy values must be rewritten to placeholders before publishing.
# Each entry: (relative path inside snapshot, [(pattern, replacement), ...])
POST_COPY_REWRITES: list[tuple[str, list[tuple[re.Pattern[str], str]]]] = [
    (
        ".env.example",
        [
            (
                re.compile(r"^GPT55_BASE_URL\s*=.*$", re.MULTILINE),
                "GPT55_BASE_URL=https://your-gpt-proxy.example.com/v1",
            ),
        ],
    ),
]


def apply_rewrites(out_dir: Path) -> list[str]:
    """Apply placeholder rewrites and return list of (rel_path) actually changed."""
    changed: list[str] = []
    for rel, rules in POST_COPY_REWRITES:
        path = out_dir / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        new_text = text
        for pat, repl in rules:
            new_text = pat.sub(repl, new_text)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed.append(rel)
    return changed


def scan_secrets(out_dir: Path) -> list[tuple[Path, str, str]]:
    """Return [(path, pattern, sample_line)] for any suspected secret in the snapshot."""
    findings: list[tuple[Path, str, str]] = []
    for path in out_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_EXTS and path.name not in {".gitignore", ".env.example"}:
            continue
        rel = path.relative_to(out_dir).as_posix()
        if rel in SECRET_SCAN_ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pat in SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                if line_end == -1:
                    line_end = len(text)
                sample = text[line_start:line_end].strip()[:200]
                findings.append((path, pat.pattern, sample))
                break
    return findings


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export a sanitized public snapshot for open-sourcing.")
    p.add_argument(
        "--out",
        type=Path,
        default=Path(r"F:\chatbot-public"),
        help="Output directory (default: F:\\chatbot-public).",
    )
    p.add_argument("--dry-run", action="store_true", help="List planned copies without writing.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Wipe the output directory before writing if it already exists.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out: Path = args.out.resolve()

    try:
        out.relative_to(ROOT)
        print(f"[ERROR] Output dir {out} is inside the repo {ROOT}. Pick a sibling path.", file=sys.stderr)
        return 2
    except ValueError:
        pass  # good: outside repo

    plan = build_plan(out)
    print(f"Plan: {len(plan)} files. Source: {ROOT}. Target: {out}")

    if args.dry_run:
        for src, dst in plan[:50]:
            print(f"  {src.relative_to(ROOT)} -> {dst.relative_to(out.parent)}")
        if len(plan) > 50:
            print(f"  ... ({len(plan) - 50} more)")
        return 0

    if out.exists():
        if not args.force:
            print(f"[ERROR] Output dir {out} already exists. Use --force to wipe.", file=sys.stderr)
            return 2
        print(f"[INFO] Wiping existing {out}")
        shutil.rmtree(out)

    out.mkdir(parents=True)
    perform_copy(plan)
    print(f"Copied {len(plan)} files into {out}")

    rewrites = apply_rewrites(out)
    if rewrites:
        print(f"Applied placeholder rewrites to: {', '.join(rewrites)}")

    findings = scan_secrets(out)
    if findings:
        print("\n[FAIL] Secret scan found suspicious content:", file=sys.stderr)
        for path, pat, sample in findings[:20]:
            print(f"  {path.relative_to(out).as_posix()}  pattern={pat}", file=sys.stderr)
            print(f"    sample: {sample}", file=sys.stderr)
        print(
            "\nReview the snapshot, fix the source, then re-run with --force.",
            file=sys.stderr,
        )
        return 3

    print("\nSecret scan: clean.")
    print("\nNext steps (manual):")
    print(f"  cd {out}")
    print("  git init")
    print("  git add -A")
    print('  git commit -m "Initial public snapshot"')
    print("  git remote add origin <your-github-url>")
    print("  git branch -M main")
    print("  git push -u origin main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
