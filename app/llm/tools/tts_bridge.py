# -*- coding: utf-8 -*-
"""
OmniVoice bridge — 工程对接 OmniVoice 离线 TTS 软件的接口模块。

架构:
    Chatbot (Python 3.12, this module)
      -> subprocess: OmniVoice python/python.exe tts_headless.py
        -> OmniVoice diffusion language model + Higgs audio tokenizer
      <- stdout JSON: {"ok": true, "paths": [...], "durations": [...]}

三种模式:
  - Voice Design: 提供由系统人设解析出的 instruct 描述
  - Voice Clone:   提供 ref_audio + ref_text 克隆声音
  - Auto Voice:    都不提供,模型自动选择声音

特点:
  - 离线运行 (本地缓存模型)
  - 中文/英文/日文/韩文等 600+ 语言
  - 输出 24000 Hz WAV 文件

使用:
    from app.llm.tools.tts_bridge import tts_design, tts_clone, tts_auto

    r = tts_design("你好世界", instruct=system_persona_voice)
    if r.ok:
        print(r.paths)  # ["F:\\chatbot\\ominvioce\\_tts_out_0000.wav"]
"""

from __future__ import annotations

import os, sys, json, subprocess, logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union, Optional

log = logging.getLogger(__name__)

# ---- Paths ----
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_OMNI_DIR = _PROJECT_ROOT / "ominvioce"
# 2026-05-11 F8 修: 跨平台 runtime 解析。Windows 用 python.exe,Linux/Mac 用 python3。
# 支持环境变量 OMNI_VOICE_RUNTIME 覆盖。
def _resolve_omni_runtime() -> Path:
    env_override = os.environ.get("OMNI_VOICE_RUNTIME", "").strip()
    if env_override:
        return Path(env_override)
    if sys.platform.startswith("win"):
        return _OMNI_DIR / "python" / "python.exe"
    # Linux/Mac
    candidates = [
        _OMNI_DIR / "python" / "python3",
        _OMNI_DIR / "python" / "bin" / "python3",
        Path("/usr/bin/python3"),
        Path("/usr/local/bin/python3"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return _OMNI_DIR / "python" / "python3"

_OMNI_RUNTIME = _resolve_omni_runtime()
_TTS_SCRIPT = _OMNI_DIR / "tts_headless.py"

# ---- Timeout ----
_DEFAULT_TIMEOUT = 300  # model loading + generation can take a while


@dataclass
class TtsResult:
    """TTS result returned by all public functions."""
    ok: bool
    paths: list[str] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    error: str = ""

    def __bool__(self) -> bool:
        return self.ok

    @property
    def path(self) -> str:
        """Convenience: first output path (most common case: single text)."""
        return self.paths[0] if self.paths else ""

    @property
    def duration(self) -> float:
        """Convenience: first output duration in seconds."""
        return self.durations[0] if self.durations else 0.0


def _call_headless(
    request: dict,
    timeout: int = _DEFAULT_TIMEOUT,
    cwd: Optional[str] = None,
) -> TtsResult:
    """Execute tts_headless.py via OmniVoice's bundled Python and parse JSON result.

    2026-05-18 P171: 加 cwd 参数。OmniVoice 子进程不带 cwd 时继承 chatbot 进程 CWD
    (= 项目根目录), 它自己写 `_tts_out_0.wav` 时是写到根目录而非 workspace。
    现在调用方传 cwd=workspace_dir, 即使 OmniVoice 忽略 `output` 参数, 文件也会
    落到 workspace 内 → 主线程能直接交付, 无需 shutil.move 兜底。
    """
    if not _OMNI_RUNTIME.is_file():
        return TtsResult(ok=False, error=f"OmniVoice runtime not found: {_OMNI_RUNTIME}")
    if not _TTS_SCRIPT.is_file():
        return TtsResult(ok=False, error=f"TTS script not found: {_TTS_SCRIPT}")

    cmd = [str(_OMNI_RUNTIME), str(_TTS_SCRIPT), "--json-stdin"]
    stdin_str = json.dumps(request, ensure_ascii=False)

    try:
        proc = subprocess.run(
            cmd,
            input=stdin_str,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            cwd=cwd,  # 2026-05-18 P171: workspace_dir → OmniVoice 即使忽略 output 也落 workspace
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return TtsResult(ok=False, error=f"TTS timeout after {timeout}s")
    except FileNotFoundError:
        return TtsResult(ok=False, error=f"Runtime missing: {_OMNI_RUNTIME}")
    except Exception as e:
        return TtsResult(ok=False, error=f"subprocess error: {e}")

    stdout = proc.stdout.strip() if proc.stdout else ""
    stderr = proc.stderr.strip() if proc.stderr else ""

    if stderr:
        # OmniVoice logs are verbose but useful for debugging
        log.debug("TTS stderr (%d lines)", stderr.count("\n") + 1)

    if not stdout:
        # 2026-05-09 BUG FIX: 把 stderr 末段并入错误信息,真实失败原因不再丢失。
        # OmniVoice 日志虽冗长但出错信息(model load fail / CUDA OOM / 文件不存在等)
        # 都在 stderr,旧版只报 rc 用户和模型都看不出真原因。
        err_msg = f"TTS produced no output (rc={proc.returncode})"
        if stderr:
            err_msg += f"; stderr tail: {stderr[-400:]}"
        return TtsResult(ok=False, error=err_msg)

    json_line = stdout.split("\n", 1)[0].strip()

    try:
        data = json.loads(json_line)
    except json.JSONDecodeError as e:
        return TtsResult(ok=False, error=f"JSON parse error: {e} (raw: {json_line[:200]})")

    if data.get("ok"):
        # 2026-05-17 Round 14k: normalize 相对路径
        # 实测 OmniVoice 有时返绝对路径 (F:\chatbot\ominvioce\_tts_out_0.wav),
        # 有时返相对路径 (_voice_xxx.wav). 后者 os.path.isfile cwd 解析失败.
        # 此处统一返绝对路径 (相对 OmniVoice cwd, 即 _OMNI_DIR).
        raw_paths = data.get("paths", [])
        norm_paths = []
        output_hint = request.get("output")
        output_dir = None
        if output_hint:
            output_dir = output_hint if os.path.isdir(output_hint) else os.path.dirname(output_hint)
        for p in raw_paths:
            if not p:
                norm_paths.append(p)
                continue
            if os.path.isabs(p):
                norm_paths.append(p)
                continue
            candidates = []
            if output_dir:
                candidates.append(os.path.join(output_dir, p))
            candidates.append(str(_OMNI_DIR / p))
            candidates.append(os.path.abspath(p))
            norm_paths.append(next((c for c in candidates if os.path.isfile(c)), candidates[0]))
        return TtsResult(
            ok=True,
            paths=norm_paths,
            durations=data.get("durations", []),
        )
    else:
        return TtsResult(ok=False, error=data.get("error", "unknown TTS error"))


# ---- Public API ----


def tts_design(
    text: Union[str, list[str]],
    instruct: str,
    *,
    language: Optional[str] = None,
    speed: Optional[float] = None,
    duration: Optional[float] = None,
    output: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    cwd: Optional[str] = None,
) -> TtsResult:
    """Voice design mode — describe the desired voice style.

    Args:
        text: Text to speak (single string or list for batch).
        instruct: Voice style description resolved by the system persona layer.
        language: Language name (e.g. "Chinese", "English", "Japanese").
        speed: Speaking speed (>1 faster, <1 slower).
        duration: Fixed output duration in seconds.
        output: Output WAV path or directory. Auto-generated if None.
        timeout: Max seconds to wait (default 300).

    Returns:
        TtsResult with .ok, .paths, .durations, .error
    """
    if not text:
        return TtsResult(ok=False, error="empty text")
    if not (instruct or "").strip():
        return TtsResult(ok=False, error="missing system voice profile")

    req = {"text": text, "instruct": instruct}
    if language:
        req["language"] = language
    if speed is not None:
        req["speed"] = speed
    if duration is not None:
        req["duration"] = duration
    if output:
        req["output"] = output

    return _call_headless(req, timeout=timeout, cwd=cwd)


def tts_clone(
    text: Union[str, list[str]],
    ref_audio: str,
    ref_text: Optional[str] = None,
    *,
    language: Optional[str] = None,
    speed: Optional[float] = None,
    output: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    cwd: Optional[str] = None,
) -> TtsResult:
    """Voice cloning mode — clone voice from reference audio.

    Args:
        text: Text to speak.
        ref_audio: Path to reference audio file (WAV, MP3, etc).
        ref_text: Transcript of reference audio. Auto-transcribed if None
            (requires ASR model to be loaded — adds ~2s).
        language: Language name.
        speed: Speaking speed factor.
        output: Output WAV path.
        timeout: Max seconds to wait.

    Returns:
        TtsResult with .ok, .paths, .durations, .error
    """
    if not text:
        return TtsResult(ok=False, error="empty text")
    if not ref_audio:
        return TtsResult(ok=False, error="ref_audio is required for voice cloning")

    req = {"text": text, "ref_audio": ref_audio}
    if ref_text:
        req["ref_text"] = ref_text
    if language:
        req["language"] = language
    if speed is not None:
        req["speed"] = speed
    if output:
        req["output"] = output

    return _call_headless(req, timeout=timeout, cwd=cwd)


def tts_auto(
    text: Union[str, list[str]],
    *,
    language: Optional[str] = None,
    speed: Optional[float] = None,
    output: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    cwd: Optional[str] = None,
) -> TtsResult:
    """Auto voice mode — model picks a natural voice automatically.

    Args:
        text: Text to speak.
        language: Language name.
        speed: Speaking speed factor.
        output: Output WAV path.
        timeout: Max seconds to wait.

    Returns:
        TtsResult with .ok, .paths, .durations, .error
    """
    if not text:
        return TtsResult(ok=False, error="empty text")

    req = {"text": text}
    if language:
        req["language"] = language
    if speed is not None:
        req["speed"] = speed
    if output:
        req["output"] = output

    return _call_headless(req, timeout=timeout, cwd=cwd)


# ---- Health check ----


def is_available() -> bool:
    """Check if OmniVoice runtime and TTS script are both accessible."""
    return _OMNI_RUNTIME.is_file() and _TTS_SCRIPT.is_file()


def version() -> str:
    """Return OmniVoice version string if available."""
    import importlib.util

    # Try to read version from omnivoice package
    init_py = _OMNI_DIR / "omnivoice" / "__init__.py"
    if init_py.is_file():
        try:
            text = init_py.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("__version__"):
                    return line.split('"')[1] if '"' in line else line.split("'")[1]
        except Exception:
            pass

    # Fallback: read from setup.py / pyproject.toml
    for fname in ["setup.py", "pyproject.toml"]:
        fpath = _OMNI_DIR / fname
        if fpath.is_file():
            try:
                text = fpath.read_text(encoding="utf-8")
                import re
                m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', text)
                if m:
                    return m.group(1)
            except Exception:
                pass

    return "unknown"
