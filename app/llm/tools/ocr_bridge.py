# -*- coding: utf-8 -*-
"""
OCR bridge — 使用 MinerU 热服务 OCR，并按 fast/balanced/accurate 单入口逐级增强。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from app.config import settings

# MinerU 在检测到图像区域但提取不到文字时，会在输出 .md 中写入占位图片引用，形如
#   ![](images/<hash>.jpg)
# 这不是 OCR 文本——若把它当成 text 返回给 LLM，score=1.0 会让模型误以为识别成功，
# 实际却只拿到一个无意义的文件名串。此正则用于识别并剥离这类占位符。
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _strip_markdown_images(text: str) -> str:
    """Remove `![alt](path)` markdown image references."""
    return _MD_IMAGE_RE.sub("", text)


def _has_text_content(text: str) -> bool:
    """True iff text has real characters, not just markdown image refs / whitespace."""
    stripped = _strip_markdown_images(text).strip()
    return bool(stripped)

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_UMI_DIR = _PROJECT_ROOT / "umi-ocr" / "Umi-OCR_Paddle_v2.1.5"
_OCR_SCRIPT = _PROJECT_ROOT / "umi-ocr" / "ocr_headless.py"
_UMI_WORKER_SCRIPT = _PROJECT_ROOT / "umi-ocr" / "ocr_worker.py"
_MINERU_DIR = _PROJECT_ROOT / "mineru"
_MINERU_CLIENT = _MINERU_DIR / "py310" / "Lib" / "site-packages" / "mineru" / "cli" / "client.py"
_MINERU_HF_DIR = _MINERU_DIR / "hf"
_MINERU_PIPELINE_REPO = "models--opendatalab--PDF-Extract-Kit-1.0"
_MINERU_VLM_REPO = "models--opendatalab--MinerU2.5-Pro-2604-1.2B"

_DEFAULT_TIMEOUT = 180
_ARGV_BASE64_SAFE_LIMIT = 16 * 1024
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".tiff"}
_OFFICE_SUFFIXES = {".docx", ".pptx", ".xlsx"}
_MINERU_SUFFIXES = _IMAGE_SUFFIXES | _OFFICE_SUFFIXES | {".pdf"}
_LONG_IMAGE_MIN_HEIGHT = 3000
_LONG_IMAGE_MAX_SLICE_HEIGHT = 1600
_LONG_IMAGE_OVERLAP = 120
_MINERU_GPU_LOCK_FILE = _MINERU_DIR / ".mineru_gpu.lock"
_MINERU_SERVICE_START_LOCK_FILE = _MINERU_DIR / ".mineru_service_start.lock"


@dataclass
class OcrResult:
    ok: bool
    text: str = ""
    score: float = 0.0
    error: str = ""
    engine: str = ""
    engine_config: dict[str, str | bool] = field(default_factory=dict)
    elapsed_ms: int = 0
    tier: str = ""
    quality_flags: list[dict] = field(default_factory=list)
    raw_text_path: str = ""
    raw_text_len: int = 0
    folded_spans: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    next_tier: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class MineruOcrConfig:
    backend: str
    method: str
    lang: str
    formula: bool
    table: bool
    image_analysis: bool
    formula_ch_support: bool
    mask_inline_formula: bool
    device_mode: str
    disable_torch_compile: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "backend": self.backend,
            "method": self.method,
            "lang": self.lang,
            "formula": self.formula,
            "table": self.table,
            "image_analysis": self.image_analysis,
            "formula_model": "pp_formulanet_plus_m" if self.formula_ch_support else "unimernet_small",
            "mask_inline_formula": self.mask_inline_formula,
            "device_mode": self.device_mode,
            "disable_torch_compile": self.disable_torch_compile,
        }


@dataclass(frozen=True)
class OcrTierConfig:
    tier: str
    engine: str
    mineru: MineruOcrConfig | None = None
    lmdeploy_backend: str = ""
    timeout: int = _DEFAULT_TIMEOUT
    long_image_max_slice_height: int | None = None
    long_image_overlap: int | None = None
    max_image_width: int | None = None
    force_single_image: bool = False
    preprocess_scale: float = 1.0
    preprocess_sharpen: bool = False
    preprocess_edge_enhance: bool = False
    env_overrides: dict[str, str] | None = None


_OCR_TIERS = {"fast", "balanced", "accurate"}
_OCR_TIER_ORDER = ["fast", "balanced", "accurate"]
_TIER_ENV = threading.local()
_RAW_OCR_DIR = _PROJECT_ROOT / "output" / "ocr_raw"
_OCR_CACHE_DIR = _PROJECT_ROOT / "output" / "ocr_cache"
_OCR_CACHE_LOCK_DIR = _OCR_CACHE_DIR / ".locks"
_OCR_CACHE_VERSION = 6
_OCR_CACHE_LOCKS: dict[str, threading.Lock] = {}
_OCR_CACHE_LOCKS_GUARD = threading.Lock()
_MATH_OR_EXAM_RE = re.compile(r"(积分|极限|导数|矩阵|行列式|方程|函数|证明|计算|解答|填空|选择题|\\\\int|\\\\sum|\\\\lim|[∫∑∮√])")


# OCR 环境/配置/工具 helper 已抽离到 ocr_env.py(2026-05-20 重构);re-export 兼容。
from app.llm.tools.ocr_env import (  # noqa: E402,F401
    _env_int,
    _env_float,
    _env_bool,
    _flag,
    _temporary_environ,
    _subprocess_creationflags,
    _file_sha256,
    _normalize_text,
)


@contextmanager
def _temporary_mineru_config(config: MineruOcrConfig, *, lmdeploy_backend: str = ""):
    overrides = {
        "MINERU_OCR_BACKEND": config.backend,
        "MINERU_OCR_METHOD": config.method,
        "MINERU_OCR_LANG": config.lang,
        "MINERU_OCR_FORMULA": str(config.formula).lower(),
        "MINERU_OCR_TABLE": str(config.table).lower(),
        "MINERU_OCR_IMAGE_ANALYSIS": str(config.image_analysis).lower(),
        "MINERU_FORMULA_CH_SUPPORT": str(config.formula_ch_support).lower(),
        "MINERU_OCR_DET_MASK_INLINE_FORMULA_ENABLE": str(config.mask_inline_formula).lower(),
        "MINERU_DEVICE_MODE": config.device_mode,
        "MINERU_DISABLE_TORCH_COMPILE": str(config.disable_torch_compile).lower(),
    }
    for key in (
        "MINERU_TORCH_NUM_THREADS",
        "MINERU_TORCH_COMPILE",
        "MINERU_OCR_USE_ANGLE_CLS",
        "MINERU_OCR_CLS_MODEL",
        "MINERU_OCR_CLS_BATCH_NUM",
        "MINERU_OCR_CLS_THRESH",
        "MINERU_VL_PROMPT_DEFAULT",
        "MINERU_VL_PROMPT_LAYOUT",
        "MINERU_VL_PROMPT_TABLE",
        "MINERU_VL_PROMPT_EQUATION",
        "MINERU_VL_PROMPT_IMAGE",
        "MINERU_VL_PROMPT_CHART",
        "MINERU_VL_TEMPERATURE",
        "MINERU_VL_TOP_P",
        "MINERU_VL_TOP_K",
        "MINERU_VL_PRESENCE_PENALTY",
        "MINERU_VL_FREQUENCY_PENALTY",
        "MINERU_VL_REPETITION_PENALTY",
        "MINERU_VL_NO_REPEAT_NGRAM_SIZE",
        "MINERU_VL_MAX_NEW_TOKENS",
        "MINERU_VL_TABLE_MAX_NEW_TOKENS",
        "MINERU_VL_EQUATION_MAX_NEW_TOKENS",
    ):
        value = os.environ.get(key)
        if value is not None:
            overrides[key] = value
    if lmdeploy_backend:
        overrides["lmdeploy_backend"] = lmdeploy_backend
        overrides["MINERU_LMDEPLOY_BACKEND"] = lmdeploy_backend
    with _temporary_environ(overrides):
        yield


def _resolve_umi_runtime() -> Path:
    env_override = os.environ.get("UMI_OCR_RUNTIME", "").strip()
    if env_override:
        return Path(env_override)
    if sys.platform.startswith("win"):
        return _UMI_DIR / "UmiOCR-data" / "runtime" / "python.exe"
    candidates = [
        _UMI_DIR / "UmiOCR-data" / "runtime" / "python3",
        _UMI_DIR / "UmiOCR-data" / "runtime" / "bin" / "python3",
        Path("/usr/bin/python3"),
        Path("/usr/local/bin/python3"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return _UMI_DIR / "UmiOCR-data" / "runtime" / "python3"


def _resolve_mineru_runtime() -> Path:
    env_override = os.environ.get("MINERU_PYTHON", "").strip()
    if env_override:
        return Path(env_override)
    if sys.platform.startswith("win"):
        return _MINERU_DIR / "py310" / "python.exe"
    return _MINERU_DIR / "py310" / "bin" / "python"


_UMI_RUNTIME = _resolve_umi_runtime()
_MINERU_RUNTIME = _resolve_mineru_runtime()








def _mineru_ocr_config(path: Path | None = None) -> MineruOcrConfig:
    override = getattr(_TIER_ENV, "mineru_config", None)
    if override is not None:
        return override
    ext = path.suffix.lower() if path is not None else ""
    is_image = ext in _IMAGE_SUFFIXES or not ext
    backend = _mineru_backend_default()
    method_default = "ocr" if is_image else "auto"
    return MineruOcrConfig(
        backend=backend,
        method=os.environ.get("MINERU_OCR_METHOD", method_default).strip() or method_default,
        lang=os.environ.get("MINERU_OCR_LANG", "ch").strip() or "ch",
        formula=_env_bool("MINERU_OCR_FORMULA", True),
        table=_env_bool("MINERU_OCR_TABLE", True),
        image_analysis=_env_bool("MINERU_OCR_IMAGE_ANALYSIS", True),
        formula_ch_support=_env_bool("MINERU_FORMULA_CH_SUPPORT", True),
        mask_inline_formula=_env_bool("MINERU_OCR_DET_MASK_INLINE_FORMULA_ENABLE", True),
        device_mode=os.environ.get("MINERU_DEVICE_MODE", "cuda").strip() or "cuda",
        disable_torch_compile=_env_bool("MINERU_DISABLE_TORCH_COMPILE", True),
    )


def _mineru_backend_default() -> str:
    return os.environ.get("MINERU_OCR_BACKEND", "pipeline").strip() or "pipeline"


def _mineru_lmdeploy_backend_default() -> str:
    return os.environ.get("MINERU_ACCURATE_LMDEPLOY_BACKEND", "pytorch").strip() or "pytorch"


def _default_mineru_config(path: Path | None = None, *, backend: str, formula: bool, table: bool, image_analysis: bool, disable_torch_compile: bool = True) -> MineruOcrConfig:
    ext = path.suffix.lower() if path is not None else ""
    method = "ocr" if ext in _IMAGE_SUFFIXES or not ext else "auto"
    return MineruOcrConfig(
        backend=backend,
        method=method,
        lang=os.environ.get("MINERU_OCR_LANG", "ch").strip() or "ch",
        formula=formula,
        table=table,
        image_analysis=image_analysis,
        formula_ch_support=True,
        mask_inline_formula=True,
        device_mode=os.environ.get("MINERU_DEVICE_MODE", "cuda").strip() or "cuda",
        disable_torch_compile=disable_torch_compile,
    )


def _ocr_tier_config(tier: str, path: Path | None = None) -> OcrTierConfig:
    normalized = tier if tier in _OCR_TIER_ORDER else "fast"
    if normalized == "fast":
        return OcrTierConfig(tier="fast", engine="legacy", timeout=60)
    if normalized == "balanced":
        return OcrTierConfig(
            tier="balanced",
            engine="mineru",
            mineru=_default_mineru_config(path, backend=_mineru_backend_default(), formula=True, table=True, image_analysis=False),
            lmdeploy_backend=_mineru_lmdeploy_backend_default(),
            timeout=max(_DEFAULT_TIMEOUT, 120),
            max_image_width=800,
            force_single_image=True,
        )
    if normalized == "accurate":
        return OcrTierConfig(
            tier="accurate",
            engine="mineru",
            mineru=_default_mineru_config(path, backend=_mineru_backend_default(), formula=True, table=True, image_analysis=False),
            lmdeploy_backend=_mineru_lmdeploy_backend_default(),
            timeout=max(_DEFAULT_TIMEOUT, 240),
            long_image_max_slice_height=2400,
            long_image_overlap=160,
        )
    return _ocr_tier_config("fast", path)




def _latest_snapshot(repo_dir: Path) -> Path | None:
    snapshots = repo_dir / "snapshots"
    if not snapshots.is_dir():
        return None
    candidates = [p for p in snapshots.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _mineru_local_model_config() -> Path | None:
    pipeline = _latest_snapshot(_MINERU_HF_DIR / _MINERU_PIPELINE_REPO)
    vlm = _latest_snapshot(_MINERU_HF_DIR / _MINERU_VLM_REPO)
    if pipeline is None or vlm is None:
        return None

    cfg = _MINERU_DIR / "mineru.local.json"
    data = {
        "config_version": "1.3.1",
        "models-dir": {
            "pipeline": str(pipeline),
            "vlm": str(vlm),
        },
    }
    try:
        existing = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else None
    except Exception:
        existing = None
    if existing != data:
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _mineru_env(config: MineruOcrConfig | None = None) -> dict[str, str]:
    config = config or _mineru_ocr_config()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cfg = _mineru_local_model_config()
    if cfg is not None:
        env.setdefault("MINERU_MODEL_SOURCE", "local")
        env.setdefault("MINERU_TOOLS_CONFIG_JSON", str(cfg))
        env.setdefault("HF_HOME", str(_MINERU_HF_DIR))
        env.setdefault("HUGGINGFACE_HUB_CACHE", str(_MINERU_HF_DIR))
    env["MINERU_OCR_BACKEND"] = config.backend
    env["MINERU_OCR_FORMULA"] = "true" if config.formula else "false"
    env["MINERU_OCR_TABLE"] = "true" if config.table else "false"
    env["MINERU_OCR_IMAGE_ANALYSIS"] = "true" if config.image_analysis else "false"
    env["MINERU_DEVICE_MODE"] = config.device_mode
    env["MINERU_API_MAX_CONCURRENT_REQUESTS"] = (
        os.environ.get("MINERU_API_MAX_CONCURRENT_REQUESTS", str(settings.mineru_concurrency)).strip()
        or str(settings.mineru_concurrency)
    )
    env["MINERU_PROCESSING_WINDOW_SIZE"] = os.environ.get("MINERU_PROCESSING_WINDOW_SIZE", "1").strip() or "1"
    env["MINERU_FORMULA_CH_SUPPORT"] = "true" if config.formula_ch_support else "false"
    env["MINERU_OCR_DET_MASK_INLINE_FORMULA_ENABLE"] = "true" if config.mask_inline_formula else "false"
    if config.disable_torch_compile:
        env["TORCH_COMPILE_DISABLE"] = "1"
        env["TORCHDYNAMO_DISABLE"] = "1"
        env["ACCELERATE_DYNAMO_BACKEND"] = "NO"
        env["MINERU_PATCH_TORCH_COMPILE_DISABLE"] = "1"
    # 2026-05-18 P167: MinerU set_lmdeploy_backend() on Windows reads
    # os.getenv("lmdeploy_backend") (lowercase, no MINERU_ prefix) due to an
    # upstream bug. Without this, hybrid/VLM backends crash with
    # "Unsupported lmdeploy backend: None". Setting the lowercase var as a
    # workaround so hybrid-auto-engine can resolve to pytorch backend.
    if sys.platform == "win32":
        lmdeploy_backend = os.environ.get("MINERU_LMDEPLOY_BACKEND") or os.environ.get("lmdeploy_backend") or "pytorch"
        env.setdefault("lmdeploy_backend", lmdeploy_backend)
        env.setdefault("MINERU_LMDEPLOY_DEVICE", "cuda")
        env.setdefault("MINERU_LMDEPLOY_BACKEND", lmdeploy_backend)
    return env


def _legacy_available() -> bool:
    return _UMI_RUNTIME.is_file() and _OCR_SCRIPT.is_file()


def _mineru_available() -> bool:
    return _MINERU_RUNTIME.is_file() and _MINERU_CLIENT.is_file()


# ── 2026-05-18 P167: persistent background service discovery ────────────
_PORT_FILE = _MINERU_DIR / ".mineru_api_port"
_PID_FILE = _MINERU_DIR / ".mineru_api_pid"
_CONFIG_FILE = _MINERU_DIR / ".mineru_api_config.json"
_DEFAULT_MINERU_API_PORT = 51111


def _effective_mineru_vlm_preload(backend: str) -> str:
    raw = os.environ.get("MINERU_API_ENABLE_VLM_PRELOAD", "false").strip().lower() or "false"
    if raw == "auto":
        return "true" if backend in {"vlm-auto-engine", "hybrid-auto-engine"} else "false"
    return "true" if raw in {"1", "true", "yes", "on"} else "false"


def _mineru_service_config(config: MineruOcrConfig) -> dict[str, str]:
    return {
        "backend": config.backend,
        "formula": str(config.formula).lower(),
        "table": str(config.table).lower(),
        "image_analysis": str(config.image_analysis).lower(),
        "formula_ch_support": str(config.formula_ch_support).lower(),
        "mask_inline_formula": str(config.mask_inline_formula).lower(),
        "device": config.device_mode,
        "disable_torch_compile": str(config.disable_torch_compile).lower(),
        "angle_cls": os.environ.get("MINERU_OCR_USE_ANGLE_CLS", "true").strip().lower() or "true",
        "angle_cls_model": os.environ.get("MINERU_OCR_CLS_MODEL", "ch_ptocr_mobile_v2.0_cls_infer.pth").strip() or "ch_ptocr_mobile_v2.0_cls_infer.pth",
        "angle_cls_thresh": os.environ.get("MINERU_OCR_CLS_THRESH", "0.9").strip() or "0.9",
        "vlm_preload": _effective_mineru_vlm_preload(config.backend),
        "max_concurrent": (
            os.environ.get("MINERU_API_MAX_CONCURRENT_REQUESTS", str(settings.mineru_concurrency)).strip()
            or str(settings.mineru_concurrency)
        ),
        "processing_window": os.environ.get("MINERU_PROCESSING_WINDOW_SIZE", "1").strip() or "1",
    }


def mineru_background_service_args(config: MineruOcrConfig, *, port: int = 51111) -> list[str]:
    service_config = _mineru_service_config(config)
    return [
        "--port", str(port),
        "--backend", service_config["backend"],
        "--formula", service_config["formula"],
        "--table", service_config["table"],
        "--image-analysis", service_config["image_analysis"],
        "--formula-ch-support", service_config["formula_ch_support"],
        "--mask-inline-formula", service_config["mask_inline_formula"],
        "--device", service_config["device"],
        "--disable-torch-compile", service_config["disable_torch_compile"],
        "--angle-cls", service_config["angle_cls"],
        "--angle-cls-model", service_config["angle_cls_model"],
        "--angle-cls-thresh", service_config["angle_cls_thresh"],
        "--vlm-preload", service_config["vlm_preload"],
        "--max-concurrent", service_config["max_concurrent"],
        "--processing-window", service_config["processing_window"],
    ]


def _read_mineru_service_config() -> dict[str, str] | None:
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    normalized = {str(k): str(v) for k, v in data.items()}
    if "vlm_preload" in normalized:
        normalized["vlm_preload"] = "true" if normalized["vlm_preload"].strip().lower() in {"1", "true", "yes", "on"} else "false"
    return normalized


def _mineru_http_alive(port: int, *, timeout: float = 2.0) -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=timeout)
        return True
    except Exception:
        return False


def _record_mineru_service_state(port: int, config: MineruOcrConfig) -> None:
    try:
        _PORT_FILE.write_text(str(port), encoding="ascii")
        _CONFIG_FILE.write_text(
            json.dumps(_mineru_service_config(config), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _record_mineru_service_pid(pid: int) -> None:
    try:
        _PID_FILE.write_text(str(int(pid)), encoding="ascii")
    except OSError:
        pass


def clear_mineru_service_state() -> None:
    for path in (_PORT_FILE, _PID_FILE, _CONFIG_FILE):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _terminate_stale_mineru_processes(*, keep_pids: set[int] | None = None) -> None:
    """Best-effort cleanup of orphaned MinerU runtime processes on Windows."""
    if sys.platform != "win32":
        return
    keep = {int(pid) for pid in (keep_pids or set()) if int(pid) > 0}
    ps = r"""
$keep = @KEEP_PIDS@
$procs = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -eq 'python.exe' -and
  $_.CommandLine -like '*\mineru\py310\python.exe*' -and
  (
    $_.CommandLine -like '*multiprocessing-fork*' -or
    $_.CommandLine -like '*mineru.cli.fast_api*' -or
    $_.CommandLine -like '*mineru.cli.client*'
  )
}
foreach ($p in $procs) {
  if ($keep -contains [int]$p.ProcessId) { continue }
  if ($keep -contains [int]$p.ParentProcessId) { continue }
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
"""
    ps = ps.replace("@KEEP_PIDS@", "@(" + ",".join(str(pid) for pid in sorted(keep)) + ")")
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        log.debug("failed to cleanup stale MinerU processes", exc_info=True)


def _start_mineru_api_process(config: MineruOcrConfig, *, port: int = _DEFAULT_MINERU_API_PORT) -> subprocess.Popen:
    """Start the single persistent MinerU API process.

    Do not wrap it in a separate launcher process: users monitor VRAM by
    process count, and the old bg_service.py -> fast_api chain looked like two
    MinerU instances even when only one API server was doing work.
    """
    cmd = [
        str(_MINERU_RUNTIME),
        "-m",
        "mineru.cli.fast_api",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_MINERU_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_mineru_env(config),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    _record_mineru_service_state(port, config)
    _record_mineru_service_pid(proc.pid)
    return proc


def _mineru_bg_api_url(config: MineruOcrConfig | None = None) -> str | None:
    """Return the API URL of a compatible persistent mineru-api service, if alive."""
    expected = _mineru_service_config(config) if config is not None else None
    ports: list[int] = []
    if _PORT_FILE.is_file():
        try:
            ports.append(int(_PORT_FILE.read_text(encoding="utf-8-sig").strip()))
        except (ValueError, OSError):
            pass
    if _DEFAULT_MINERU_API_PORT not in ports:
        ports.append(_DEFAULT_MINERU_API_PORT)

    for port in ports:
        recorded_config = _read_mineru_service_config()
        if expected is not None and recorded_config is not None and recorded_config != expected:
            continue
        if _mineru_http_alive(port, timeout=2):
            if config is not None and recorded_config != expected:
                _record_mineru_service_state(port, config)
            return f"http://127.0.0.1:{port}"
    return None


def _wait_mineru_bg_api_url(config: MineruOcrConfig, *, timeout: int) -> str | None:
    deadline = time.monotonic() + min(max(1, timeout), 30)
    while True:
        url = _mineru_bg_api_url(config)
        if url:
            return url
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


_UMI_WORKER_LOCK = threading.Lock()
_UMI_WORKER_PROC: subprocess.Popen | None = None


def _start_umi_worker_locked(timeout: int = 60) -> subprocess.Popen | None:
    global _UMI_WORKER_PROC
    if _UMI_WORKER_PROC is not None and _UMI_WORKER_PROC.poll() is None:
        return _UMI_WORKER_PROC
    if not _UMI_RUNTIME.is_file() or not _UMI_WORKER_SCRIPT.is_file():
        return None
    try:
        proc = subprocess.Popen(
            [str(_UMI_RUNTIME), str(_UMI_WORKER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=_subprocess_creationflags(),
        )
        assert proc.stdout is not None
        deadline = time.time() + timeout
        line = ""
        while time.time() < deadline:
            line = proc.stdout.readline().strip()
            if line:
                break
            if proc.poll() is not None:
                break
        if not line:
            proc.kill()
            return None
        data = json.loads(line)
        if not data.get("ready"):
            proc.kill()
            return None
        _UMI_WORKER_PROC = proc
        return proc
    except Exception:
        try:
            proc.kill()  # type: ignore[name-defined]
        except Exception:
            pass
        _UMI_WORKER_PROC = None
        return None


def warm_umi_worker(timeout: int = 60) -> bool:
    with _UMI_WORKER_LOCK:
        return _start_umi_worker_locked(timeout=timeout) is not None


def stop_umi_worker() -> None:
    global _UMI_WORKER_PROC
    with _UMI_WORKER_LOCK:
        proc = _UMI_WORKER_PROC
        _UMI_WORKER_PROC = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write(json.dumps({"cmd": "stop"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout=3)
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def _call_umi_worker(payload: dict, timeout: int) -> OcrResult | None:
    with _UMI_WORKER_LOCK:
        proc = _start_umi_worker_locked(timeout=timeout)
        if proc is None or proc.stdin is None or proc.stdout is None:
            return None
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline().strip()
            if not line:
                return None
            data = json.loads(line)
        except Exception:
            stop_umi_worker()
            return None
    if data.get("ok"):
        return OcrResult(ok=True, text=str(data.get("text", "")), score=float(data.get("score", 0) or 0), engine="umi")
    return OcrResult(ok=False, error=str(data.get("error", "Umi worker failed")), engine="umi")


def _umi_worker_active() -> bool:
    proc = _UMI_WORKER_PROC
    return proc is not None and proc.poll() is None and _UMI_WORKER_LOCK.locked()


def _tts_headless_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        proc = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine", "/FORMAT:CSV"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_subprocess_creationflags(),
            timeout=5,
        )
    except Exception:
        return False
    needle = str(_PROJECT_ROOT / "ominvioce" / "tts_headless.py").lower().replace("/", "\\")
    for line in (proc.stdout or "").splitlines():
        low = line.lower().replace("/", "\\")
        if "tts_headless.py" in low or needle in low:
            return True
    return False


def _wait_gpu_competitors_idle(timeout: float = 900.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if not _umi_worker_active() and not _tts_headless_running():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _terminate_idle_umi_processes() -> None:
    global _UMI_WORKER_PROC
    if _umi_worker_active():
        return
    _UMI_WORKER_PROC = None
    if sys.platform != "win32":
        return
    try:
        proc = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/FORMAT:CSV"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_subprocess_creationflags(),
            timeout=5,
        )
    except Exception:
        return
    needle = str(_UMI_DIR).lower().replace("/", "\\")
    for line in (proc.stdout or "").splitlines():
        low = line.lower().replace("/", "\\")
        if needle not in low and "ocr_worker.py" not in low and "ocr_headless.py" not in low:
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if not parts:
            continue
        pid = parts[-1]
        if not pid.isdigit():
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", pid, "/T", "/F"],
                capture_output=True,
                creationflags=_subprocess_creationflags(),
                timeout=8,
            )
        except Exception:
            pass


def _terminate_umi_processes() -> None:
    _terminate_idle_umi_processes()


def _call_legacy_headless(args: list[str], timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    from app.llm.tools.gpu_resources import gpu_resource_scope
    with gpu_resource_scope("umiocr"):
        return _call_legacy_headless_unlocked(args, timeout=timeout)


def _call_legacy_headless_unlocked(args: list[str], timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if not _legacy_available():
        return OcrResult(ok=False, error=f"Umi-OCR runtime not found: {_UMI_RUNTIME}")

    if len(args) == 1 and not str(args[0]).startswith("--"):
        worker_result = _call_umi_worker({"path": str(Path(args[0]).resolve()), "timeout": timeout}, timeout)
        if worker_result is not None:
            return worker_result
    elif len(args) >= 2 and args[0] == "--base64":
        worker_result = _call_umi_worker({"base64": args[1], "timeout": timeout}, timeout)
        if worker_result is not None:
            return worker_result

    cmd = [str(_UMI_RUNTIME), str(_OCR_SCRIPT)] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=_subprocess_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return OcrResult(ok=False, error=f"OCR timeout after {timeout}s")
    except FileNotFoundError:
        return OcrResult(ok=False, error=f"Runtime missing: {_UMI_RUNTIME}")
    except OSError as e:
        return OcrResult(ok=False, error=f"subprocess OSError: {e}")
    except Exception as e:
        return OcrResult(ok=False, error=f"subprocess error: {e}")

    stdout = proc.stdout.strip() if proc.stdout else ""
    stderr = proc.stderr.strip() if proc.stderr else ""

    if stderr and "PPOCR" not in stderr:
        log.warning("OCR stderr: %s", stderr[:200])

    if not stdout:
        stderr_clean = "\n".join(ln for ln in stderr.splitlines() if "PPOCR" not in ln).strip()
        err_msg = f"OCR produced no output (rc={proc.returncode})"
        if stderr_clean:
            err_msg += f"; stderr tail: {stderr_clean[-300:]}"
        return OcrResult(ok=False, error=err_msg)

    json_line = stdout.split("\n", 1)[0].strip()
    try:
        data = json.loads(json_line)
    except json.JSONDecodeError as e:
        return OcrResult(ok=False, error=f"JSON parse error: {e} (raw: {json_line[:200]})")

    if data.get("ok"):
        return OcrResult(ok=True, text=data.get("text", ""), score=data.get("score", 0.0))
    return OcrResult(ok=False, error=data.get("error", "unknown OCR error"))




def _mineru_error_excerpt(proc: subprocess.CompletedProcess[str]) -> str:
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    parts: list[str] = []
    if stderr:
        parts.append(stderr[-500:])
    if stdout:
        parts.append(stdout[-500:])
    return " | ".join(p for p in parts if p)


def _collect_mineru_text(output_dir: Path, stem: str) -> str:
    candidates = sorted(output_dir.rglob(f"{stem}.md"))
    if not candidates:
        candidates = sorted(output_dir.rglob("*.md"))
    for candidate in candidates:
        try:
            text = _normalize_text(candidate.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if text:
            return text
    return ""


def _copy_result(result: OcrResult, **updates) -> OcrResult:
    data = {
        "ok": result.ok,
        "text": result.text,
        "score": result.score,
        "error": result.error,
        "engine": result.engine,
        "engine_config": dict(result.engine_config),
        "elapsed_ms": result.elapsed_ms,
        "tier": result.tier,
        "quality_flags": list(result.quality_flags),
        "raw_text_path": result.raw_text_path,
        "raw_text_len": result.raw_text_len,
        "folded_spans": list(result.folded_spans),
        "candidates": list(result.candidates),
        "next_tier": result.next_tier,
    }
    data.update(updates)
    return OcrResult(**data)




@contextmanager
def _cross_process_file_lock(lock_path: Path, *, timeout: float = 600.0, poll_interval: float = 0.25):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import msvcrt

        with open(lock_path, "a+b") as f:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for lock: {lock_path}")
                    time.sleep(poll_interval)
            try:
                yield
            finally:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        return

    import fcntl

    with open(lock_path, "a+b") as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for lock: {lock_path}")
                time.sleep(poll_interval)
        try:
            yield
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def mineru_service_start_lock(timeout: float = 120.0):
    return _cross_process_file_lock(_MINERU_SERVICE_START_LOCK_FILE, timeout=timeout)


def _mineru_gpu_lock(timeout: float = 900.0):
    return _cross_process_file_lock(_MINERU_GPU_LOCK_FILE, timeout=timeout)


def _mineru_exclusive_process_lock_needed() -> bool:
    try:
        return int(settings.mineru_concurrency) <= 1
    except (TypeError, ValueError):
        return True


def _needs_tier_upgrade(result: OcrResult) -> bool:
    if not result.ok:
        return True
    text = _strip_markdown_images(result.text or "").strip()
    if not text:
        return True
    if len(text) < int(os.environ.get("OCR_AUTO_UPGRADE_MIN_TEXT_CHARS", "120") or "120"):
        return True
    if result.quality_flags or result.folded_spans:
        return True
    if _MATH_OR_EXAM_RE.search(text) and result.score < float(os.environ.get("OCR_AUTO_UPGRADE_MATH_SCORE", "0.72") or "0.72"):
        return True
    if result.score and result.score < float(os.environ.get("OCR_AUTO_UPGRADE_SCORE", "0.60") or "0.60"):
        return True
    return False


def _should_try_legacy_complement(result: OcrResult, source_path: Path) -> bool:
    """Use Umi as a cheap complement when MinerU produces suspiciously sparse text."""
    if source_path.suffix.lower() not in _IMAGE_SUFFIXES:
        return False
    if not result.ok or result.engine != "mineru":
        return False
    text = _strip_markdown_images(result.text or "").strip()
    if len(text) < int(os.environ.get("OCR_LEGACY_COMPLEMENT_MAX_TEXT_CHARS", "80") or "80"):
        return True
    digits = len(re.findall(r"\d+(?:\.\d+)?", text))
    words = len(re.findall(r"[A-Za-z]{2,}", text))
    if len(text) < 160 and digits <= 1 and words <= 6:
        return True
    return False


def _merge_ocr_complement(primary: OcrResult, complement: OcrResult) -> OcrResult:
    if not complement.ok:
        flags = list(primary.quality_flags)
        flags.append(_flag("ocr_legacy_complement_failed", engine=complement.engine or "umi", error=complement.error[:180]))
        return _copy_result(primary, quality_flags=flags)
    primary_text = (primary.text or "").strip()
    complement_text = (complement.text or "").strip()
    if not complement_text:
        return primary
    primary_norm = re.sub(r"\s+", "", primary_text).lower()
    complement_norm = re.sub(r"\s+", "", complement_text).lower()
    flags = list(primary.quality_flags)
    flags.append(_flag("ocr_legacy_complement_used", engine=complement.engine or "umi", text_len=len(complement_text)))
    engine_config = dict(primary.engine_config)
    engine_config["legacy_complement_engine"] = complement.engine or "umi"
    engine_config["legacy_complement_text_len"] = len(complement_text)
    candidates = list(primary.candidates or [])
    candidates.append(_candidate_summary(_copy_result(complement, tier=f"{primary.tier or 'accurate'}_legacy_complement")))
    if complement_norm and complement_norm not in primary_norm:
        merged_text = (
            primary_text
            + "\n\n--- Umi OCR complement ---\n"
            + complement_text
        ).strip()
    else:
        merged_text = primary_text or complement_text
    return _copy_result(
        primary,
        text=merged_text,
        score=max(primary.score, complement.score),
        quality_flags=flags,
        engine_config=engine_config,
        candidates=candidates,
    )


def _ocr_cache_lock(cache_key: str) -> threading.Lock:
    with _OCR_CACHE_LOCKS_GUARD:
        lock = _OCR_CACHE_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _OCR_CACHE_LOCKS[cache_key] = lock
        return lock


def _ocr_cache_path(file_hash: str, tier: str) -> Path:
    safe_tier = tier if tier in _OCR_TIERS else "fast"
    return _OCR_CACHE_DIR / file_hash / f"{safe_tier}.json"


def _ocr_cache_file_lock(cache_key: str):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", cache_key)
    return _cross_process_file_lock(_OCR_CACHE_LOCK_DIR / f"{safe}.lock", timeout=900)


def _ocr_result_to_dict(result: OcrResult) -> dict:
    return {
        "ok": result.ok,
        "text": result.text,
        "score": result.score,
        "error": result.error,
        "engine": result.engine,
        "engine_config": dict(result.engine_config),
        "elapsed_ms": result.elapsed_ms,
        "tier": result.tier,
        "quality_flags": list(result.quality_flags),
        "raw_text_path": result.raw_text_path,
        "raw_text_len": result.raw_text_len,
        "folded_spans": list(result.folded_spans),
        "candidates": [],
        "next_tier": "",
    }


def _ocr_result_from_dict(data: dict) -> OcrResult:
    return OcrResult(
        ok=bool(data.get("ok", False)),
        text=str(data.get("text", "") or ""),
        score=float(data.get("score", 0.0) or 0.0),
        error=str(data.get("error", "") or ""),
        engine=str(data.get("engine", "") or ""),
        engine_config=dict(data.get("engine_config") or {}),
        elapsed_ms=int(data.get("elapsed_ms", 0) or 0),
        tier=str(data.get("tier", "") or ""),
        quality_flags=list(data.get("quality_flags") or []),
        raw_text_path=str(data.get("raw_text_path", "") or ""),
        raw_text_len=int(data.get("raw_text_len", 0) or 0),
        folded_spans=list(data.get("folded_spans") or []),
        candidates=[],
        next_tier="",
    )


def _load_cached_tier(file_hash: str, tier: str) -> OcrResult | None:
    path = _ocr_cache_path(file_hash, tier)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("cache_version") != _OCR_CACHE_VERSION:
        return None
    result_data = payload.get("result")
    if not isinstance(result_data, dict):
        return None
    result = _ocr_result_from_dict(result_data)
    if tier in {"balanced", "accurate"} and result.engine != "mineru":
        return None
    engine_config = dict(result.engine_config)
    engine_config["cache_hit"] = True
    engine_config["cache_tier"] = tier
    return _copy_result(result, engine_config=engine_config, elapsed_ms=0)


def _load_cached_at_or_above(file_hash: str, requested_tier: str, max_tier: str) -> OcrResult | None:
    """Return the strongest cached result whose tier is >= requested and <= max_tier."""
    req_index = _OCR_TIER_ORDER.index(requested_tier)
    max_index = _OCR_TIER_ORDER.index(max_tier)
    for cached_tier in reversed(_OCR_TIER_ORDER[req_index : max_index + 1]):
        cached = _load_cached_tier(file_hash, cached_tier)
        if cached is not None:
            engine_config = dict(cached.engine_config)
            engine_config["cache_satisfies_requested_tier"] = requested_tier
            return _copy_result(cached, tier=cached.tier or cached_tier, engine_config=engine_config)
    return None


def _store_cached_tier(file_hash: str, tier: str, result: OcrResult, source_path: Path) -> None:
    if not result.ok:
        return
    if tier in {"balanced", "accurate"} and result.engine != "mineru":
        return
    path = _ocr_cache_path(file_hash, tier)
    payload = {
        "cache_version": _OCR_CACHE_VERSION,
        "created_at": int(time.time()),
        "source": {
            "sha256": file_hash,
            "size": source_path.stat().st_size if source_path.exists() else 0,
            "mtime": source_path.stat().st_mtime if source_path.exists() else 0,
            "suffix": source_path.suffix.lower(),
        },
        "result": _ocr_result_to_dict(result),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f"{tier}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            if "tmp" in locals():
                os.unlink(tmp)
        except OSError:
            pass


def _with_tier_metadata(result: OcrResult, *, requested: str, max_tier: str) -> OcrResult:
    req_index = _OCR_TIER_ORDER.index(requested)
    max_index = _OCR_TIER_ORDER.index(max_tier)
    next_tier = _OCR_TIER_ORDER[req_index + 1] if req_index + 1 < len(_OCR_TIER_ORDER) and req_index < max_index else ""
    result = _copy_result(result, tier=result.tier or requested, candidates=[])
    return _copy_result(result, candidates=[_candidate_summary(result)], next_tier=next_tier if result.ok else "")




def _compact_pattern(pattern: str, limit: int = 80) -> str:
    pattern = re.sub(r"\s+", " ", pattern).strip()
    return pattern[:limit] + ("..." if len(pattern) > limit else "")


def _add_span(spans: list[dict], start: int, end: int, signal: str, pattern: str, repeats: int | None = None) -> None:
    if end - start < 30:
        return
    spans.append({
        "start": start,
        "end": end,
        "len": end - start,
        "signal": signal,
        "pattern": _compact_pattern(pattern),
        **({"repeats": repeats} if repeats is not None else {}),
    })


def _analyze_ocr_runaway(text: str) -> dict:
    spans: list[dict] = []
    flags: list[dict] = []
    run_start = 0
    for idx in range(1, len(text) + 1):
        if idx < len(text) and text[idx] == text[run_start]:
            continue
        if idx - run_start >= 1000:
            _add_span(spans, run_start, idx, "repeated_chars", text[run_start], idx - run_start)
        run_start = idx

    token_re = re.compile(r"\\[A-Za-z]+|[\w㐀-鿿㉠-㊿]+|[^\s\w㐀-鿿]", re.S)
    tokens = [(m.group(0), m.start(), m.end()) for m in token_re.finditer(text)]
    for size in (1, 2, 3, 4, 5, 6, 8, 10, 12):
        i = 0
        limit = len(tokens) - size
        while i <= limit:
            window = tuple(tok for tok, _, _ in tokens[i:i + size])
            repeats = 1
            j = i + size
            while j <= limit and tuple(tok for tok, _, _ in tokens[j:j + size]) == window:
                repeats += 1
                j += size
            if repeats >= 8:
                start = tokens[i][1]
                end = tokens[j - 1][2]
                pattern = "".join(window)
                _add_span(spans, start, end, "repeated_token_window", pattern, repeats)
                i = j
            else:
                i += 1

    for m in re.finditer(r"((?:\s*(?:\\hline|空|&\s*&|\|\s*\|)\s*){12,})", text):
        _add_span(spans, m.start(), m.end(), "repeated_table_or_blank", m.group(1)[:40], None)

    repeated_factor_patterns = [
        r"(?:\s*(?:[+*×]|\\\*)\s*\([^\n()]{1,40}\)){12,}",
        r"(?:\s*(?:[+*×]|\\\*)\s*\{[^\n{}]{1,40}\}){12,}",
        r"(?:\s*(?:[+*×]|\\\*)\s*\\[A-Za-z]+(?:_\{[^}]{1,20}\}|\^[{]?[^}\s]{1,20}[}]?)?){12,}",
    ]
    for pattern in repeated_factor_patterns:
        for m in re.finditer(pattern, text):
            _add_span(spans, m.start(), m.end(), "repeated_formula_factors", m.group(0)[:80], None)

    for m in re.finditer(r"((?:\([^\n()]{1,40}\)\s*[*×]\s*){12,})", text):
        _add_span(spans, m.start(), m.end(), "repeated_formula_factors", m.group(1)[:80], None)

    cursor = 0
    for line in text.splitlines(keepends=True):
        clean = line.strip()
        if len(clean) >= 1200:
            _add_span(spans, cursor, cursor + len(line), "abnormally_long_line", clean[:80], None)
        cursor += len(line)

    placeholder_count = len(_MD_IMAGE_RE.findall(text))
    if placeholder_count:
        flags.append(_flag("ocr_image_placeholder", count=placeholder_count))

    spans.sort(key=lambda s: (s["start"], -(s["end"] - s["start"])))
    merged: list[dict] = []
    for span in spans:
        if not merged or span["start"] > merged[-1]["end"]:
            merged.append(span)
            continue
        current = merged[-1]
        if span["end"] > current["end"]:
            current["end"] = span["end"]
            current["len"] = current["end"] - current["start"]
        if span["len"] > current.get("len", 0):
            current.update({k: v for k, v in span.items() if k not in {"start", "end"}})
    runaway_chars = sum(s["end"] - s["start"] for s in merged)
    if runaway_chars:
        flags.append(_flag("ocr_runaway_repetition", span_count=len(merged), folded_chars=runaway_chars, ratio=round(runaway_chars / max(1, len(text)), 4)))
    if len(text) >= 12000 and runaway_chars / max(1, len(text)) >= 0.25:
        flags.append(_flag("ocr_text_length_blowup", text_len=len(text), repeated_chars=runaway_chars))
    return {"spans": merged, "flags": flags, "runaway_score": runaway_chars}


def _write_raw_ocr(text: str) -> str:
    if not text:
        return ""
    try:
        _RAW_OCR_DIR.mkdir(parents=True, exist_ok=True)
        path = _RAW_OCR_DIR / f"raw_{int(time.time() * 1000)}_{os.getpid()}.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)
    except OSError:
        return ""


def _fold_ocr_runaway(text: str, analysis: dict | None = None) -> tuple[str, list[dict]]:
    analysis = analysis or _analyze_ocr_runaway(text)
    spans = analysis.get("spans") or []
    if not spans:
        return text, []
    parts: list[str] = []
    folded: list[dict] = []
    pos = 0
    for span in spans:
        start = int(span["start"])
        end = int(span["end"])
        parts.append(text[pos:start])
        signal = span.get("signal")
        details = [
            f"signal={signal}",
            f"ocr_output_span={start}-{end}",
            f"chars={end-start}",
        ]
        if signal == "repeated_chars" and span.get("repeats") is not None:
            details.append(f"repeated_char={span.get('pattern')}")
            details.append(f"repeat_count={span.get('repeats')}")
        else:
            details.append(f"pattern={span.get('pattern')}")
        marker = f"[OCR输出重复异常已折叠: {', '.join(details)}]"
        parts.append(marker)
        folded.append(dict(span))
        pos = end
    parts.append(text[pos:])
    return "".join(parts), folded


def _apply_ocr_quality_guard(result: OcrResult, *, source_text: str | None = None) -> OcrResult:
    if not result.ok or not result.text:
        return result
    raw_text = source_text if source_text is not None else result.text
    analysis = _analyze_ocr_runaway(raw_text)
    folded_text, folded_spans = _fold_ocr_runaway(raw_text, analysis)
    raw_path = ""
    raw_len = len(raw_text)
    if folded_spans and folded_text != raw_text:
        raw_path = _write_raw_ocr(raw_text)
    return _copy_result(
        result,
        text=folded_text,
        quality_flags=[*result.quality_flags, *analysis.get("flags", [])],
        raw_text_path=raw_path or result.raw_text_path,
        raw_text_len=raw_len if folded_spans else result.raw_text_len,
        folded_spans=[*result.folded_spans, *folded_spans],
    )


def _ocr_readability_score(result: OcrResult) -> float:
    text = _strip_markdown_images(result.text or "").strip()
    if not result.ok or not text:
        return -100.0
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cjk = len(re.findall(r"[㐀-鿿]", text))
    latin_words = len(re.findall(r"[A-Za-z]{2,}", text))
    digits = len(re.findall(r"\d", text))
    meaningful_chars = cjk + sum(len(m.group(0)) for m in re.finditer(r"[A-Za-z]{2,}", text)) + digits
    single_char_lines = sum(1 for line in lines if len(line) <= 2)
    punctuation_noise = len(re.findall(r"[^\w\s㐀-鿿，。！？；：、（）【】《》“”‘’·—…,.!?;:()\[\]{}<>+\-*/=_%^$#@&|\\]", text))
    line_penalty = single_char_lines / max(1, len(lines))
    density = meaningful_chars / max(1, len(text))
    return (result.score * 10.0) + min(len(text), 500) / 40.0 + density * 8.0 + cjk * 0.08 + latin_words * 0.15 - line_penalty * 4.0 - punctuation_noise * 0.2


def _ocr_result_looks_rotated_bad(result: OcrResult) -> bool:
    text = _strip_markdown_images(result.text or "").strip()
    if not result.ok:
        return True
    if not text:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(text) <= 40 and len(lines) >= 5:
        single_char_lines = sum(1 for line in lines if len(line) <= 2)
        if single_char_lines / max(1, len(lines)) >= 0.65:
            return True
    cjk = len(re.findall(r"[㐀-鿿]", text))
    latin_words = len(re.findall(r"[A-Za-z]{2,}", text))
    digits = len(re.findall(r"\d", text))
    meaningful = cjk + latin_words * 2 + digits
    if len(text) <= 80 and result.score < 0.72 and meaningful / max(1, len(text)) < 0.55:
        return True
    return False


def _prepare_rotated_image(path: Path, degrees: int) -> tuple[Path, callable | None]:
    if degrees % 360 == 0 or path.suffix.lower() not in _IMAGE_SUFFIXES:
        return path, None
    try:
        from PIL import Image
    except Exception:
        return path, None
    temp_path: Path | None = None
    try:
        with Image.open(path) as img:
            if getattr(img, "is_animated", False):
                img.seek(0)
            source = img.convert("RGB")
            rotated = source.rotate(degrees, expand=True)
            fd, raw = tempfile.mkstemp(prefix=f"ocr_rotate_{degrees}_", suffix=".jpg")
            os.close(fd)
            temp_path = Path(raw)
            rotated.save(temp_path, format="JPEG", quality=94)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return path, None

    def _cleanup() -> None:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    return temp_path, _cleanup


def _mark_orientation(result: OcrResult, degrees: int, tried: list[int]) -> OcrResult:
    engine_config = dict(result.engine_config)
    engine_config["rotation_degrees"] = str(degrees)
    if len(tried) > 1:
        engine_config["rotation_tried"] = ",".join(str(d) for d in tried)
    quality_flags = list(result.quality_flags)
    if degrees:
        quality_flags.append(_flag("ocr_orientation_corrected", rotation_degrees=degrees))
    return _copy_result(result, engine_config=engine_config, quality_flags=quality_flags)


def _orientation_probe_order(eight_way: bool = False) -> tuple[int, ...]:
    if eight_way:
        return (90, 270, 180, 45, 315, 135, 225)
    return (90, 270, 180)


def _run_image_orientation_variants(path: Path, cfg: OcrTierConfig, timeout: int, runner) -> OcrResult:
    first = runner(path, timeout)
    if path.suffix.lower() not in _IMAGE_SUFFIXES or not _ocr_result_looks_rotated_bad(first):
        return _mark_orientation(first, 0, [0])
    best = first
    best_degrees = 0
    tried = [0]
    for degrees in _orientation_probe_order(eight_way=True):
        rotated_path, cleanup = _prepare_rotated_image(path, degrees)
        if rotated_path == path:
            continue
        tried.append(degrees)
        try:
            candidate = runner(rotated_path, timeout)
        finally:
            if cleanup is not None:
                cleanup()
        if _ocr_readability_score(candidate) > _ocr_readability_score(best):
            best = candidate
            best_degrees = degrees
        if not _ocr_result_looks_rotated_bad(best) and _ocr_readability_score(best) >= _ocr_readability_score(first) + 3.0:
            break
    return _mark_orientation(best, best_degrees, tried)


def _detect_image_orientation_with_umi(path: Path, timeout: int) -> tuple[int, list[int]]:
    first = _ocr_with_legacy_file(path, timeout=min(timeout, 60))
    first = _apply_ocr_quality_guard(_copy_result(first, tier="orientation", engine=first.engine or "umi"))
    if not _ocr_result_looks_rotated_bad(first):
        return 0, [0]
    best = first
    best_degrees = 0
    tried = [0]
    for degrees in _orientation_probe_order(eight_way=False):
        rotated_path, cleanup = _prepare_rotated_image(path, degrees)
        if rotated_path == path:
            continue
        tried.append(degrees)
        try:
            candidate = _ocr_with_legacy_file(rotated_path, timeout=min(timeout, 60))
            candidate = _apply_ocr_quality_guard(_copy_result(candidate, tier="orientation", engine=candidate.engine or "umi"))
        finally:
            if cleanup is not None:
                cleanup()
        if _ocr_readability_score(candidate) > _ocr_readability_score(best):
            best = candidate
            best_degrees = degrees
        if not _ocr_result_looks_rotated_bad(best) and _ocr_readability_score(best) >= _ocr_readability_score(first) + 3.0:
            break
    if not _ocr_result_looks_rotated_bad(best):
        return best_degrees, tried
    for degrees in _orientation_probe_order(eight_way=True)[3:]:
        rotated_path, cleanup = _prepare_rotated_image(path, degrees)
        if rotated_path == path:
            continue
        tried.append(degrees)
        try:
            candidate = _ocr_with_legacy_file(rotated_path, timeout=min(timeout, 60))
            candidate = _apply_ocr_quality_guard(_copy_result(candidate, tier="orientation", engine=candidate.engine or "umi"))
        finally:
            if cleanup is not None:
                cleanup()
        if _ocr_readability_score(candidate) > _ocr_readability_score(best):
            best = candidate
            best_degrees = degrees
    return best_degrees, tried


def _run_with_umi_orientation_probe(path: Path, cfg: OcrTierConfig, timeout: int, runner) -> OcrResult:
    if path.suffix.lower() not in _IMAGE_SUFFIXES:
        return runner(path, timeout)
    degrees, tried = _detect_image_orientation_with_umi(path, timeout)
    oriented_path = path
    cleanup = None
    if degrees:
        oriented_path, cleanup = _prepare_rotated_image(path, degrees)
    try:
        result = runner(oriented_path, timeout)
    finally:
        if cleanup is not None:
            cleanup()
    return _mark_orientation(result, degrees, tried)


def _candidate_summary(result: OcrResult) -> dict:
    return {
        "tier": result.tier,
        "ok": result.ok,
        "engine": result.engine,
        "elapsed_ms": result.elapsed_ms,
        "text_len": len(result.text or ""),
        "raw_text_len": result.raw_text_len or len(result.text or ""),
        "quality_flags": [flag.get("signal", "") for flag in result.quality_flags],
        **({"error": result.error[:300]} if not result.ok and result.error else {}),
    }


def _prepare_image_for_tier(path: Path, cfg: OcrTierConfig) -> tuple[Path, callable | None]:
    if path.suffix.lower() not in _IMAGE_SUFFIXES:
        return path, None
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return path, None

    temp_path: Path | None = None
    try:
        with Image.open(path) as img:
            if getattr(img, "is_animated", False):
                img.seek(0)
            source = img.convert("RGB")
            width, height = source.size
            target_width = width
            if cfg.max_image_width and cfg.max_image_width > 0:
                target_width = min(target_width, cfg.max_image_width)
            if cfg.preprocess_scale and cfg.preprocess_scale > 1.0:
                target_width = max(target_width, round(width * cfg.preprocess_scale))
            if target_width == width and not cfg.preprocess_sharpen and not cfg.preprocess_edge_enhance:
                return path, None
            target_height = max(1, round(height * target_width / width))
            prepared = source.resize((target_width, target_height), Image.Resampling.LANCZOS) if target_width != width else source
            if cfg.preprocess_edge_enhance:
                prepared = prepared.filter(ImageFilter.EDGE_ENHANCE_MORE)
            if cfg.preprocess_sharpen:
                prepared = prepared.filter(ImageFilter.UnsharpMask(radius=0.8, percent=90, threshold=3))
            fd, raw = tempfile.mkstemp(prefix="ocr_tier_", suffix=".jpg")
            os.close(fd)
            temp_path = Path(raw)
            prepared.save(temp_path, format="JPEG", quality=92)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return path, None

    def _cleanup() -> None:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    return temp_path, _cleanup


def _run_tier(path: Path, cfg: OcrTierConfig, timeout: int) -> OcrResult:
    if cfg.engine == "legacy":
        def _run_legacy_once(candidate_path: Path, candidate_timeout: int) -> OcrResult:
            result = _ocr_with_legacy_file(candidate_path, timeout=min(candidate_timeout, cfg.timeout))
            result = _copy_result(result, tier=cfg.tier, engine=result.engine or "umi")
            return _apply_ocr_quality_guard(result)

        return _run_image_orientation_variants(path, cfg, timeout, _run_legacy_once)
    if cfg.mineru is None:
        return OcrResult(ok=False, error=f"tier {cfg.tier} has no MinerU config", tier=cfg.tier)

    def _run_mineru_once(candidate_path: Path, candidate_timeout: int) -> OcrResult:
        old_config = getattr(_TIER_ENV, "mineru_config", None)
        old_slice_height = os.environ.get("MINERU_LONG_IMAGE_MAX_SLICE_HEIGHT")
        old_slice_overlap = os.environ.get("MINERU_LONG_IMAGE_OVERLAP")
        old_slice_min_height = os.environ.get("MINERU_LONG_IMAGE_MIN_HEIGHT")
        old_env_overrides = {key: os.environ.get(key) for key in (cfg.env_overrides or {})}
        prepared_path, cleanup_prepared = _prepare_image_for_tier(candidate_path, cfg)
        _TIER_ENV.mineru_config = cfg.mineru
        if cfg.long_image_max_slice_height is not None:
            os.environ["MINERU_LONG_IMAGE_MAX_SLICE_HEIGHT"] = str(cfg.long_image_max_slice_height)
        if cfg.long_image_overlap is not None:
            os.environ["MINERU_LONG_IMAGE_OVERLAP"] = str(cfg.long_image_overlap)
        if cfg.force_single_image:
            os.environ["MINERU_LONG_IMAGE_MIN_HEIGHT"] = "99999"
        for key, value in (cfg.env_overrides or {}).items():
            os.environ[key] = value
        try:
            with _temporary_mineru_config(cfg.mineru, lmdeploy_backend=cfg.lmdeploy_backend):
                if prepared_path.suffix.lower() not in _MINERU_SUFFIXES:
                    result = OcrResult(
                        ok=False,
                        error=f"tier {cfg.tier} requires MinerU-compatible input: {prepared_path.suffix.lower()}",
                        engine="mineru",
                        engine_config=cfg.mineru.as_dict(),
                    )
                elif _needs_long_image_slicing(prepared_path):
                    result = _ocr_long_image_with_mineru(prepared_path, timeout=max(candidate_timeout, cfg.timeout))
                else:
                    result = _ocr_with_mineru_file(prepared_path, timeout=max(candidate_timeout, cfg.timeout))
        finally:
            if cleanup_prepared is not None:
                cleanup_prepared()
            if old_slice_height is None:
                os.environ.pop("MINERU_LONG_IMAGE_MAX_SLICE_HEIGHT", None)
            else:
                os.environ["MINERU_LONG_IMAGE_MAX_SLICE_HEIGHT"] = old_slice_height
            if old_slice_overlap is None:
                os.environ.pop("MINERU_LONG_IMAGE_OVERLAP", None)
            else:
                os.environ["MINERU_LONG_IMAGE_OVERLAP"] = old_slice_overlap
            if old_slice_min_height is None:
                os.environ.pop("MINERU_LONG_IMAGE_MIN_HEIGHT", None)
            else:
                os.environ["MINERU_LONG_IMAGE_MIN_HEIGHT"] = old_slice_min_height
            for key, value in old_env_overrides.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if old_config is None:
                try:
                    delattr(_TIER_ENV, "mineru_config")
                except AttributeError:
                    pass
            else:
                _TIER_ENV.mineru_config = old_config
        result = _copy_result(result, tier=cfg.tier, engine=result.engine or cfg.engine)
        return _apply_ocr_quality_guard(result)

    return _run_with_umi_orientation_probe(path, cfg, timeout, _run_mineru_once)


def ocr_file_tiered(path: Union[str, Path], *, tier: str = "fast", allow_upgrade: bool = False, max_tier: str = "accurate", timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    target = Path(path)
    if not target.is_file():
        return OcrResult(ok=False, error=f"image file not found: {target}", tier=tier)
    requested = tier if tier in _OCR_TIERS else "fast"
    max_tier = max_tier if max_tier in _OCR_TIER_ORDER else "accurate"
    req_index = _OCR_TIER_ORDER.index(requested)
    max_index = _OCR_TIER_ORDER.index(max_tier)
    if req_index > max_index:
        return OcrResult(ok=False, error=f"tier {requested} is above max_tier {max_tier}", tier=requested)

    try:
        file_hash = _file_sha256(target)
    except OSError as e:
        return OcrResult(ok=False, error=f"failed to hash image file: {e}", tier=requested)

    candidates: list[dict] = []
    best_result: OcrResult | None = None
    tiers = _OCR_TIER_ORDER[req_index : (max_index + 1 if allow_upgrade else req_index + 1)]
    for current_tier in tiers:
        cache_key = f"{file_hash}:{current_tier}"
        cached = _load_cached_at_or_above(file_hash, current_tier, max_tier)
        if cached is None:
            lock = _ocr_cache_lock(cache_key)
            with lock:
                with _ocr_cache_file_lock(cache_key):
                    cached = _load_cached_at_or_above(file_hash, current_tier, max_tier)
                    if cached is None:
                        result = _run_tier(target, _ocr_tier_config(current_tier, target), timeout)
                        result = _copy_result(result, tier=result.tier or current_tier)
                        _store_cached_tier(file_hash, current_tier, result, target)
                    else:
                        result = cached
        else:
            result = cached

        result = _copy_result(result, tier=result.tier or current_tier)
        candidates.append(_candidate_summary(result))
        best_result = result
        if result.engine_config.get("cache_hit") and result.tier in _OCR_TIER_ORDER:
            if _OCR_TIER_ORDER.index(result.tier) >= max_index:
                break
        if not allow_upgrade or current_tier == max_tier or not _needs_tier_upgrade(result):
            break

    if best_result is None:
        return OcrResult(ok=False, error="OCR produced no result", tier=requested)
    if best_result.tier == max_tier and _should_try_legacy_complement(best_result, target):
        legacy_result = _apply_ocr_quality_guard(_copy_result(
            _ocr_with_legacy_file(target, timeout=min(timeout, 60)),
            tier=f"{max_tier}_legacy_complement",
            engine="umi",
        ))
        best_result = _merge_ocr_complement(best_result, legacy_result)
    final_requested = best_result.tier if best_result.tier in _OCR_TIER_ORDER else requested
    final = _with_tier_metadata(best_result, requested=final_requested, max_tier=max_tier)
    merged_candidates = [*candidates]
    for item in getattr(best_result, "candidates", None) or []:
        if item not in merged_candidates:
            merged_candidates.append(item)
    return _copy_result(final, candidates=merged_candidates)


def _ocr_mineru_office_images(path: Path, *, timeout: int = _DEFAULT_TIMEOUT) -> str:
    result = ocr_office_images(
        path,
        max_images=int(os.environ.get("MINERU_OFFICE_OCR_MAX_IMAGES", "50") or "50"),
        per_image_timeout=timeout,
        max_workers=1,
    )
    if not result.get("ok"):
        return ""
    return _normalize_text(str(result.get("merged_text") or ""))


def _ocr_with_mineru_file(path: Path, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if not _mineru_available():
        return OcrResult(ok=False, error=f"MinerU runtime not found: {_MINERU_RUNTIME}", engine="mineru")

    return _ocr_with_mineru_file_direct(path, timeout=timeout)


def _run_mineru_client(cmd: list[str], config: MineruOcrConfig, timeout: int) -> subprocess.CompletedProcess[str]:
    if sys.platform != "win32":
        return subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=_mineru_env(config),
            creationflags=_subprocess_creationflags(),
        )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_mineru_env(config),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15,
        )
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)


def _ocr_with_mineru_file_direct(path: Path, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    from app.llm.tools.gpu_resources import gpu_resource_scope
    with gpu_resource_scope("mineru"):
        return _ocr_with_mineru_file_direct_locked(path, timeout=timeout)


def _ocr_with_mineru_file_direct_locked(path: Path, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    config = _mineru_ocr_config(path)
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="mineru_ocr_") as td:
        output_dir = Path(td) / "out"
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(_MINERU_RUNTIME),
            "-m",
            "mineru.cli.client",
            "-p",
            str(path),
            "-o",
            str(output_dir),
            "-b",
            config.backend,
            "-m",
            config.method,
            "-l",
            config.lang,
            "-f",
            str(config.formula).lower(),
            "-t",
            str(config.table).lower(),
            "--image-analysis",
            str(config.image_analysis).lower(),
        ]
        # 2026-05-18 P167: reuse persistent background service when available
        # to skip ~30 s cold-start model loading per OCR call.
        try:
            if _umi_worker_active() or _tts_headless_running():
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                return OcrResult(
                    ok=False,
                    error="GPU resource busy: UmiOCR or TTS is active; MinerU did not kill an active GPU task",
                    engine="mineru",
                    engine_config=config.as_dict(),
                    elapsed_ms=elapsed_ms,
                )
            lock_cm = (
                _mineru_gpu_lock(timeout=max(timeout, 1))
                if _mineru_exclusive_process_lock_needed()
                else nullcontext()
            )
            with lock_cm:
                if not _wait_gpu_competitors_idle(timeout=max(timeout, 1)):
                    elapsed_ms = int((time.perf_counter() - start) * 1000)
                    return OcrResult(
                        ok=False,
                        error="GPU resource busy: UmiOCR or TTS is active; MinerU did not kill an active GPU task",
                        engine="mineru",
                        engine_config=config.as_dict(),
                        elapsed_ms=elapsed_ms,
                    )
                _terminate_idle_umi_processes()
                bg_url = _wait_mineru_bg_api_url(config, timeout=timeout)
                if not bg_url:
                    elapsed_ms = int((time.perf_counter() - start) * 1000)
                    return OcrResult(
                        ok=False,
                        error="MinerU hot service unavailable or busy; not starting a cold fallback process",
                        engine="mineru",
                        engine_config=config.as_dict(),
                        elapsed_ms=elapsed_ms,
                    )
                cmd.extend(["--api-url", bg_url])
                proc = _run_mineru_client(cmd, config, timeout)
        except TimeoutError as e:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return OcrResult(ok=False, error=f"MinerU hot service lock timeout: {e}", engine="mineru", engine_config=config.as_dict(), elapsed_ms=elapsed_ms)
        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return OcrResult(ok=False, error=f"MinerU timeout after {timeout}s", engine="mineru", engine_config=config.as_dict(), elapsed_ms=elapsed_ms)
        except FileNotFoundError:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return OcrResult(ok=False, error=f"MinerU runtime missing: {_MINERU_RUNTIME}", engine="mineru", engine_config=config.as_dict(), elapsed_ms=elapsed_ms)
        except OSError as e:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return OcrResult(ok=False, error=f"MinerU subprocess OSError: {e}", engine="mineru", engine_config=config.as_dict(), elapsed_ms=elapsed_ms)
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return OcrResult(ok=False, error=f"MinerU subprocess error: {e}", engine="mineru", engine_config=config.as_dict(), elapsed_ms=elapsed_ms)

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        text = _collect_mineru_text(output_dir, path.stem)
        # 2026-05-18 P167: MinerU is the primary OCR engine.  Only treat
        # hard crashes (subprocess error, timeout, non-zero rc) as failures.
        # Soft issues (image placeholders, partial/garbled text) stay as ok=True
        if proc.returncode == 0:
            engine_config = config.as_dict()
            if path.suffix.lower() in _OFFICE_SUFFIXES:
                image_text = _ocr_mineru_office_images(path, timeout=timeout)
                if image_text:
                    text = "\n\n".join(part for part in (text, image_text) if part).strip()
                    engine_config = dict(engine_config)
                    engine_config["embedded_image_ocr"] = True
            return OcrResult(ok=True, text=text, score=1.0, engine="mineru", engine_config=engine_config, elapsed_ms=elapsed_ms)

        excerpt = _mineru_error_excerpt(proc)
        if excerpt:
            return OcrResult(ok=False, error=f"MinerU failed (rc={proc.returncode}): {excerpt}", engine="mineru", engine_config=config.as_dict(), elapsed_ms=elapsed_ms)
        return OcrResult(ok=False, error=f"MinerU failed (rc={proc.returncode})", engine="mineru", engine_config=config.as_dict(), elapsed_ms=elapsed_ms)


def _ocr_image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def _needs_long_image_slicing(path: Path) -> bool:
    if path.suffix.lower() not in _IMAGE_SUFFIXES:
        return False
    dims = _ocr_image_dimensions(path)
    if dims is None:
        return False
    width, height = dims
    min_height = _env_int("MINERU_LONG_IMAGE_MIN_HEIGHT", _LONG_IMAGE_MIN_HEIGHT, minimum=1)
    ratio = _env_float("MINERU_LONG_IMAGE_MIN_RATIO", 2.0, minimum=1.0)
    return height >= min_height and height > width * ratio


def _slice_long_image(path: Path) -> tuple[list[Path], callable]:
    from PIL import Image

    temp_dir = Path(tempfile.mkdtemp(prefix="ocr_longimg_"))
    created: list[Path] = []
    try:
        with Image.open(path) as img:
            source = img.convert("RGB")
            width, height = source.size
            max_slice_height = _env_int("MINERU_LONG_IMAGE_MAX_SLICE_HEIGHT", _LONG_IMAGE_MAX_SLICE_HEIGHT, minimum=400)
            overlap = min(_env_int("MINERU_LONG_IMAGE_OVERLAP", _LONG_IMAGE_OVERLAP, minimum=0), max_slice_height - 1)
            y = 0
            index = 1
            while y < height:
                y1 = min(height, y + max_slice_height)
                if y1 <= y:
                    break
                slice_path = temp_dir / f"slice_{index:03d}_{y}_{y1}.jpg"
                source.crop((0, y, width, y1)).save(slice_path, quality=92)
                created.append(slice_path)
                if y1 >= height:
                    break
                y = max(0, y1 - overlap)
                index += 1
    except Exception:
        for item in created:
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            temp_dir.rmdir()
        except OSError:
            pass
        raise

    def _cleanup() -> None:
        for item in created:
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            temp_dir.rmdir()
        except OSError:
            pass

    return created, _cleanup


def _ocr_long_image_with_mineru(path: Path, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    start = time.perf_counter()
    config = _mineru_ocr_config(path)
    try:
        slices, cleanup = _slice_long_image(path)
    except Exception as e:
        return OcrResult(ok=False, error=f"long image slicing failed: {e}", engine="mineru", engine_config=config.as_dict())

    try:
        if not slices:
            return _ocr_with_mineru_file_direct(path, timeout=timeout)
        texts: list[str] = []
        errors: list[str] = []
        per_slice_timeout = max(timeout, _DEFAULT_TIMEOUT)
        for idx, slice_path in enumerate(slices, 1):
            result = _ocr_with_mineru_file_direct(slice_path, timeout=per_slice_timeout)
            if result.ok:
                text = _normalize_text(result.text)
                if text:
                    texts.append(text)
            else:
                errors.append(f"slice {idx}: {result.error}")
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        combined = "\n\n".join(texts).strip()
        if combined:
            engine_config = dict(config.as_dict())
            engine_config["long_image_sliced"] = True
            engine_config["slice_count"] = str(len(slices))
            return OcrResult(ok=True, text=combined, score=1.0, engine="mineru", engine_config=engine_config, elapsed_ms=elapsed_ms)
        return OcrResult(ok=False, error="; ".join(errors) or "long image OCR produced empty text", engine="mineru", engine_config=config.as_dict(), elapsed_ms=elapsed_ms)
    finally:
        cleanup()


def _render_pdf_to_images(pdf_path: Path) -> tuple[list[Path], callable]:
    """Render PDF pages to temporary PNG files for legacy OCR fallback."""
    import fitz

    temp_dir = Path(tempfile.mkdtemp(prefix="ocr_pdf_"))
    created: list[Path] = []
    doc = fitz.open(pdf_path)
    try:
        for idx in range(len(doc)):
            page = doc.load_page(idx)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            out = temp_dir / f"page_{idx + 1}.png"
            pix.save(str(out))
            created.append(out)
    finally:
        doc.close()

    def _cleanup() -> None:
        for item in created:
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            temp_dir.rmdir()
        except OSError:
            pass

    return created, _cleanup


def _ocr_pdf_with_legacy(pdf_path: Path, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if not _legacy_available():
        return OcrResult(ok=False, error="legacy OCR unavailable for PDF fallback")
    try:
        image_paths, cleanup = _render_pdf_to_images(pdf_path)
    except Exception as e:
        return OcrResult(ok=False, error=f"PDF render failed: {e}")

    try:
        texts: list[str] = []
        for image_path in image_paths:
            result = _ocr_with_legacy_file(image_path, timeout=timeout)
            if not result.ok:
                return result
            text = _normalize_text(result.text)
            if text:
                texts.append(text)
        combined = "\n\n".join(texts).strip()
        if combined:
            return OcrResult(ok=True, text=combined, score=1.0)
        return OcrResult(ok=False, error="PDF OCR produced empty text")
    finally:
        cleanup()


def _image_suffix_from_data(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return ".pdf"
    try:
        from PIL import Image
        from io import BytesIO

        with Image.open(BytesIO(data)) as img:
            fmt = (img.format or "").lower()
    except Exception:
        return ".img"
    if fmt == "jpeg":
        return ".jpg"
    if fmt:
        return f".{fmt}"
    return ".img"


def _ensure_supported_image_path(path: Path) -> tuple[Path, callable | None]:
    ext = path.suffix.lower()
    if ext in _MINERU_SUFFIXES:
        return path, None
    try:
        from PIL import Image
    except Exception:
        return path, None

    temp_path: Path | None = None
    try:
        with Image.open(path) as img:
            if getattr(img, "is_animated", False):
                img.seek(0)
            converted = img.convert("RGB")
            fd, raw = tempfile.mkstemp(prefix="ocr_adapt_", suffix=".png")
            os.close(fd)
            temp_path = Path(raw)
            converted.save(temp_path, format="PNG")
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return path, None

    def _cleanup() -> None:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    return temp_path or path, _cleanup


def _is_legacy_supported_file(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES or path.suffix.lower() == ".pdf"


def _ocr_with_legacy_file(path: Union[str, Path], *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    target = Path(path)
    if not target.is_file():
        return OcrResult(ok=False, error=f"image file not found: {target}")
    if not _is_legacy_supported_file(target):
        return OcrResult(ok=False, error=f"legacy OCR does not support {target.suffix.lower() or 'suffixless'} files")
    if target.suffix.lower() == ".pdf":
        return _ocr_pdf_with_legacy(target, timeout=timeout)
    prepared, cleanup = _ensure_supported_image_path(target)
    try:
        return _call_legacy_headless([str(prepared)], timeout=timeout)
    finally:
        if cleanup is not None:
            cleanup()


def ocr_file(path: Union[str, Path], *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    return ocr_file_tiered(path, tier="fast", allow_upgrade=False, max_tier="fast", timeout=timeout)


def _ocr_file_uncached(path: Union[str, Path], *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    target = Path(path)
    if not target.is_file():
        return OcrResult(ok=False, error=f"image file not found: {target}")

    prepared, cleanup = _ensure_supported_image_path(target)
    try:
        if prepared.suffix.lower() in _MINERU_SUFFIXES:
            if _needs_long_image_slicing(prepared):
                mineru_result = _ocr_long_image_with_mineru(prepared, timeout=timeout)
            else:
                mineru_result = _ocr_with_mineru_file(prepared, timeout=timeout)
            # 2026-05-18 P167: MinerU is the primary engine.
            # Soft degradation (partial text, image placeholders) stays MinerU.
            if mineru_result.ok:
                return mineru_result
            legacy_result = _ocr_with_legacy_file(prepared, timeout=timeout)
            if legacy_result.ok:
                return legacy_result
            return OcrResult(ok=False, error=f"MinerU: {mineru_result.error}; fallback: {legacy_result.error}")

        return _ocr_with_legacy_file(prepared, timeout=timeout)
    finally:
        if cleanup is not None:
            cleanup()


def _ocr_via_tempfile(data: bytes, timeout: int) -> OcrResult:
    suffix = _image_suffix_from_data(data)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, prefix="ocr_", delete=False) as tf:
            tf.write(data)
            tmp_path = Path(tf.name)
        return ocr_file(tmp_path, timeout=timeout)
    except OSError as e:
        return OcrResult(ok=False, error=f"OCR tempfile fallback failed: {e}")
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def ocr_bytes(data: bytes, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if not data:
        return OcrResult(ok=False, error="empty image data")
    suffix = _image_suffix_from_data(data)
    if suffix == ".pdf":
        return _ocr_via_tempfile(data, timeout)
    if len(data) * 4 // 3 > _ARGV_BASE64_SAFE_LIMIT:
        return _ocr_via_tempfile(data, timeout)
    b64 = base64.b64encode(data).decode("ascii")
    return ocr_base64(b64, timeout=timeout)


def ocr_base64(b64: str, *, timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    if "," in b64 and "base64" in b64[:50]:
        b64 = b64.split(",", 1)[1]
    try:
        decoded = base64.b64decode(b64, validate=True)
    except Exception as e:
        return OcrResult(ok=False, error=f"invalid base64: {e}")
    if _image_suffix_from_data(decoded) == ".pdf":
        return _ocr_via_tempfile(decoded, timeout)
    if len(b64) > _ARGV_BASE64_SAFE_LIMIT:
        return _ocr_via_tempfile(decoded, timeout)
    if _mineru_available():
        mineru_result = _ocr_via_tempfile(decoded, timeout)
        if mineru_result.ok:
            return mineru_result
        legacy_result = _call_legacy_headless(["--base64", b64], timeout=timeout)
        if legacy_result.ok:
            return legacy_result
        return OcrResult(ok=False, error=f"MinerU: {mineru_result.error}; fallback: {legacy_result.error}")
    return _call_legacy_headless(["--base64", b64], timeout=timeout)


def is_available() -> bool:
    return _mineru_available() or _legacy_available()


def version() -> str:
    parts: list[str] = []
    if _mineru_available():
        parts.append("mineru")
    about_json = _UMI_DIR / "UmiOCR-data" / "about.json"
    if about_json.is_file():
        try:
            data = json.loads(about_json.read_text(encoding="utf-8"))
            v = data.get("version", {})
            parts.append(f"umi-{v.get('major', 0)}.{v.get('minor', 0)}.{v.get('patch', 0)}")
        except Exception:
            parts.append("umi")
    elif _legacy_available():
        parts.append("umi")
    return "+".join(parts) if parts else "unavailable"


async def ocr_file_scheduled(path: Union[str, Path], *, tier: str = "fast", allow_upgrade: bool = True, max_tier: str = "accurate", timeout: int = _DEFAULT_TIMEOUT) -> OcrResult:
    from app.llm.tools.registry import run_gpu_ocr
    return await run_gpu_ocr(
        ocr_file_tiered,
        path,
        tier=tier,
        allow_upgrade=allow_upgrade,
        max_tier=max_tier,
        timeout=timeout,
    )


async def ocr_office_images_scheduled(
    file_path: Union[str, Path],
    *,
    media_prefix: str = "",
    max_images: int = 50,
    max_size_mb: float = 50.0,
    per_image_timeout: int = 30,
) -> dict:
    import zipfile
    import tempfile

    target = Path(file_path)
    if not target.is_file():
        return {"ok": False, "error": f"file not found: {target}"}

    if not media_prefix:
        suffix = target.suffix.lower()
        if suffix == ".docx":
            media_prefix = "word/media/"
        elif suffix == ".pptx":
            media_prefix = "ppt/media/"
        elif suffix == ".xlsx":
            media_prefix = "xl/media/"
        else:
            return {"ok": False, "error": f"unsupported ext: {suffix}"}

    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}
    extracted: list[dict] = []
    skipped_too_large = 0
    too_large_items: list[dict] = []
    total_images = 0

    try:
        with zipfile.ZipFile(str(target), "r") as zf:
            media_files = [
                name for name in zf.namelist()
                if name.startswith(media_prefix)
                and any(name.lower().endswith(e) for e in image_exts)
            ]
            media_files.sort()
            total_images = len(media_files)
            if not media_files:
                return {"ok": True, "total_images": 0, "ocr_count": 0, "items": [], "merged_text": ""}
            for name in media_files:
                if len(extracted) >= max_images:
                    break
                try:
                    info = zf.getinfo(name)
                    size_bytes = info.file_size
                except KeyError:
                    continue
                if size_bytes > max_size_mb * 1024 * 1024:
                    skipped_too_large += 1
                    too_large_items.append({
                        "name": os.path.basename(name),
                        "size_bytes": size_bytes,
                        "text": "",
                        "skip_reason": f"too large (>{max_size_mb}MB)",
                    })
                    continue
                ext = os.path.splitext(name)[1].lower()
                try:
                    with tempfile.NamedTemporaryFile(suffix=ext, prefix="office_img_", delete=False) as tmp:
                        tmp.write(zf.read(name))
                        tmp_path = tmp.name
                except OSError as e:
                    log.warning("office image extract tempfile failed: %s", e)
                    continue
                extracted.append({"name": os.path.basename(name), "size_bytes": size_bytes, "tmp_path": tmp_path})
    except zipfile.BadZipFile:
        return {"ok": False, "error": "not a valid office (zip) file"}
    except Exception as e:
        return {"ok": False, "error": f"unexpected error: {e!r}"}

    items: list[dict] = []
    ocr_count = 0
    skipped_no_text = 0
    try:
        for item in extracted:
            try:
                result = await ocr_file_scheduled(
                    Path(item["tmp_path"]),
                    tier="fast",
                    allow_upgrade=True,
                    max_tier="accurate",
                    timeout=per_image_timeout,
                )
                text = result.text.strip() if result.ok else ""
                if text:
                    items.append({"name": item["name"], "size_bytes": item["size_bytes"], "text": text})
                    ocr_count += 1
                else:
                    items.append({
                        "name": item["name"],
                        "size_bytes": item["size_bytes"],
                        "text": "",
                        "skip_reason": result.error or "no text detected",
                    })
                    skipped_no_text += 1
            except Exception as e:
                log.warning("ocr image %s failed: %s", item["name"], e)
                items.append({
                    "name": item["name"],
                    "size_bytes": item["size_bytes"],
                    "text": "",
                    "skip_reason": f"exception: {e!r}",
                })
                skipped_no_text += 1
    finally:
        for item in extracted:
            try:
                os.unlink(item["tmp_path"])
            except OSError:
                pass

    items.extend(too_large_items)
    merged_text = "\n\n".join(f"[图 {it['name']}]\n{it['text']}" for it in items if it.get("text"))
    return {
        "ok": True,
        "total_images": total_images,
        "ocr_count": ocr_count,
        "skipped_too_large": skipped_too_large,
        "skipped_no_text": skipped_no_text,
        "items": items,
        "merged_text": merged_text,
    }


# ──────────────────────────────────────────────────────────────────
# Office (docx/pptx/xlsx) 嵌入图片 OCR (2026-05-16)
# ──────────────────────────────────────────────────────────────────

def ocr_office_images(
    file_path: Union[str, Path],
    *,
    media_prefix: str = "",
    max_images: int = 50,
    image_offset: int = 0,
    max_size_mb: float = 50.0,
    per_image_timeout: int = 30,
    max_workers: int = 4,
) -> dict:
    """提取 office 文件 (docx/pptx/xlsx) 嵌入图片并 OCR.
    
    office 文件本质是 zip:
    - docx: word/media/imageN.{png,jpg,...}
    - pptx: ppt/media/...
    - xlsx: xl/media/...
    
    Args:
        file_path: office 文件路径
        media_prefix: zip 内媒体目录前缀 (留空则按文件类型自动推断)
        max_images: 最多处理几张 (默认 50, 一般足够)
        image_offset: 从第几张嵌入图片开始处理(0-based),用于大文档分批 OCR
        max_size_mb: 单图最大 MB (默认 50, 几乎不限. 真巨型图才跳)
        per_image_timeout: 单张图 OCR timeout (默认 30s, 之前 90s 是失败默认)
        max_workers: 并发 OCR 线程数 (默认 4, 防止单进程 OCR 串行 → API stall)
    
    Returns:
        {
            "ok": True,
            "total_images": N,         # zip 内总图数
            "ocr_count": M,            # 成功 OCR 的张数
            "skipped_too_large": K,    # 太大跳过的
            "skipped_no_text": J,      # OCR 了但无文本的
            "items": [
                {"name": "image1.png", "size_bytes": 12345, "text": "..."},
                ...
            ],
            "merged_text": "[图 image1.png]\n...\n\n[图 image2.png]\n..."
        }

    2026-05-18 P172: 并发改写。之前 16 张图 × 90s = 24min serial, 90s API stall 必触发。
    现在 max_workers=4 并发, 4× 加速 + per_image_timeout 30s 防单图卡死。
    """
    import zipfile
    import tempfile
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    target = Path(file_path)
    if not target.is_file():
        return {"ok": False, "error": f"file not found: {target}"}
    
    # 自动推断 media_prefix
    if not media_prefix:
        suffix = target.suffix.lower()
        if suffix == ".docx":
            media_prefix = "word/media/"
        elif suffix == ".pptx":
            media_prefix = "ppt/media/"
        elif suffix == ".xlsx":
            media_prefix = "xl/media/"
        else:
            return {"ok": False, "error": f"unsupported ext: {suffix}"}
    
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}
    
    # 阶段 1: 提取候选图片到临时文件 (顺序, 快)
    extracted: list[dict] = []  # 每项 {"name", "size_bytes", "tmp_path"}
    skipped_too_large = 0
    too_large_items: list[dict] = []
    total_images = 0
    
    try:
        with zipfile.ZipFile(str(target), "r") as zf:
            media_files = [
                name for name in zf.namelist()
                if name.startswith(media_prefix)
                and any(name.lower().endswith(e) for e in _IMAGE_EXTS)
            ]
            media_files.sort()
            total_images = len(media_files)
            
            if not media_files:
                return {
                    "ok": True, "total_images": 0,
                    "ocr_count": 0, "items": [], "merged_text": "",
                }
            
            image_offset = max(0, int(image_offset or 0))
            selected_media_files = media_files[image_offset:]
            for name in selected_media_files:
                if len(extracted) >= max_images:
                    break
                
                try:
                    info = zf.getinfo(name)
                    size_bytes = info.file_size
                except KeyError:
                    continue
                
                if size_bytes > max_size_mb * 1024 * 1024:
                    skipped_too_large += 1
                    too_large_items.append({
                        "name": os.path.basename(name),
                        "size_bytes": size_bytes,
                        "text": "",
                        "skip_reason": f"too large (>{max_size_mb}MB)",
                    })
                    continue
                
                ext = os.path.splitext(name)[1].lower()
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=ext, prefix="office_img_", delete=False
                    ) as tmp:
                        tmp.write(zf.read(name))
                        tmp_path = tmp.name
                except OSError as e:
                    log.warning("office image extract tempfile failed: %s", e)
                    continue
                
                extracted.append({
                    "name": os.path.basename(name),
                    "size_bytes": size_bytes,
                    "tmp_path": tmp_path,
                })
    except zipfile.BadZipFile:
        return {"ok": False, "error": "not a valid office (zip) file"}
    except Exception as e:
        return {"ok": False, "error": f"unexpected error: {e!r}"}
    
    # 阶段 2: 并发 OCR 所有已提取图片
    items_by_name: dict[str, dict] = {}
    ocr_count = 0
    skipped_no_text = 0
    
    def _ocr_one(item: dict) -> dict:
        try:
            from app.llm.tools.gpu_resources import gpu_resource_scope
            with gpu_resource_scope("ocr"):
                result = ocr_file(Path(item["tmp_path"]), timeout=per_image_timeout)
            text = result.text.strip() if result.ok else ""
            if text:
                return {"name": item["name"], "size_bytes": item["size_bytes"], "text": text}
            else:
                return {
                    "name": item["name"], "size_bytes": item["size_bytes"], "text": "",
                    "skip_reason": result.error or "no text detected",
                }
        except Exception as e:
            log.warning("ocr image %s failed: %s", item["name"], e)
            return {
                "name": item["name"], "size_bytes": item["size_bytes"], "text": "",
                "skip_reason": f"exception: {e!r}",
            }
        finally:
            try:
                os.unlink(item["tmp_path"])
            except OSError:
                pass
    
    # 边界保护: 当 extracted 数较少时, max_workers 不必超过 len
    workers = max(1, min(max_workers, len(extracted)))
    if workers == 1 or len(extracted) <= 1:
        # 单图直接 inline 调, 不引入 ThreadPool 开销
        for it in extracted:
            res = _ocr_one(it)
            items_by_name[res["name"]] = res
            if res.get("text"):
                ocr_count += 1
            elif "skip_reason" in res:
                skipped_no_text += 1
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="office_ocr") as pool:
            future_to_name = {pool.submit(_ocr_one, it): it["name"] for it in extracted}
            for fut in as_completed(future_to_name):
                res = fut.result()
                items_by_name[res["name"]] = res
                if res.get("text"):
                    ocr_count += 1
                elif "skip_reason" in res:
                    skipped_no_text += 1
    
    # 按原 media_files 顺序输出 items (并发完成顺序不可预测)
    items: list[dict] = []
    for it in extracted:
        if it["name"] in items_by_name:
            items.append(items_by_name[it["name"]])
    # too-large 跳过的追加在末尾
    items.extend(too_large_items)
    
    # 合并 OCR 文本
    merged_parts = []
    for it in items:
        if it.get("text"):
            merged_parts.append(f"[图 {it['name']}]\n{it['text']}")
    merged_text = "\n\n".join(merged_parts)
    
    processed_count = len(extracted)
    next_image_offset = image_offset + processed_count
    has_more_images = next_image_offset < total_images
    return {
        "ok": True,
        "total_images": total_images,
        "image_offset": image_offset,
        "processed_images": processed_count,
        "has_more_images": has_more_images,
        **({"next_image_offset": next_image_offset} if has_more_images else {}),
        "ocr_count": ocr_count,
        "skipped_too_large": skipped_too_large,
        "skipped_no_text": skipped_no_text,
        "items": items,
        "merged_text": merged_text,
    }
