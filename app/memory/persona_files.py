"""
人设文件读取。

每个人设一个 .md 文件，放在 personas/ 目录下。
格式：

    name: <显示名>
    description: <一句话概述，用于选择界面>
    ---
    <完整人设内容>
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_PERSONAS_DIR = Path(__file__).parent.parent.parent / "personas"


@dataclass
class PersonaMeta:
    id: str          # 文件名去扩展名
    name: str        # 显示名
    description: str # 一句话概述
    filename: str    # 完整文件名


@dataclass
class PersonaFile:
    meta: PersonaMeta
    content: str     # 完整人设文本


def _split_meta_content(text: str) -> tuple[dict[str, str], str]:
    """解析文件头部 metadata 和正文。"""
    meta: dict[str, str] = {}
    content = text
    # 查找第一个 --- 分隔符
    match = re.search(r"\n---\n", text)
    if match:
        header = text[: match.start()]
        content = text[match.end():]
        for line in header.strip().split("\n"):
            line = line.strip()
            if ":" in line:
                key, _, val = line.partition(":")
                meta[key.strip()] = val.strip()
    return meta, content.strip()


def list_personas() -> list[PersonaMeta]:
    """列出所有可用人设的元信息。"""
    if not _PERSONAS_DIR.exists():
        return []
    result = []
    for f in sorted(_PERSONAS_DIR.glob("*.md")):
        pid = f.stem
        name = pid
        desc = ""
        try:
            raw = f.read_text(encoding="utf-8")
            meta, _ = _split_meta_content(raw)
            name = meta.get("name", pid)
            desc = meta.get("description", "")
        except Exception:
            pass
        result.append(PersonaMeta(id=pid, name=name, description=desc, filename=f.name))
    return result


def load_persona(persona_id: str) -> PersonaFile | None:
    """加载一个完整人设文件。"""
    path = _PERSONAS_DIR / f"{persona_id}.md"
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    meta, content = _split_meta_content(raw)
    name = meta.get("name", persona_id)
    desc = meta.get("description", "")
    return PersonaFile(
        meta=PersonaMeta(id=persona_id, name=name, description=desc, filename=path.name),
        content=content,
    )


def resolve_persona_file_by_label(label: str) -> PersonaFile | None:
    """Resolve a persona file from a stored UI/group label."""
    target = _persona_match_target(label or "")
    if not target or not _PERSONAS_DIR.exists():
        return None
    direct = load_persona((label or "").strip())
    if direct:
        return direct
    for f in sorted(_PERSONAS_DIR.glob("*.md")):
        try:
            raw = f.read_text(encoding="utf-8")
        except Exception:
            continue
        meta, _ = _split_meta_content(raw)
        names = [
            f.stem,
            meta.get("name", ""),
            *(_mojibake_variants(f.stem)),
            *(_mojibake_variants(meta.get("name", ""))),
        ]
        if any(_persona_match_target(n) == target for n in names if n):
            return load_persona(f.stem)
    return None


def find_persona_voice_sample(persona_name: str) -> Optional[Path]:
    """Return personas/<name>.wav when a persona-specific voice sample exists."""
    name = (persona_name or "").strip()
    if not name:
        return None
    path = _PERSONAS_DIR / f"{name}.wav"
    return path if path.is_file() else None


def _mojibake_variants(text: str) -> list[str]:
    variants: list[str] = []
    for enc in ("gbk", "cp936"):
        try:
            variants.append(text.encode("utf-8").decode(enc, errors="ignore"))
        except Exception:
            pass
    return variants


def _persona_match_target(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _persona_ascii_tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text or "")}


def _persona_name_candidates(persona: str, meta: dict[str, str], content: str) -> list[str]:
    candidates: list[str] = []
    name = (meta.get("name", "") or "").strip()
    if name:
        candidates.append(name)
    m = re.search(r"^\s*name\s*:\s*(.+?)\s*$", persona or "", re.IGNORECASE | re.MULTILINE)
    if m:
        candidates.append(m.group(1).strip())
    for text in (content or "", persona or ""):
        for pattern in (
            r"你是(?:一个|一位|名为)?[\"“']?([^\"“”'。\n，,]{1,24})[\"”']?(?:。|，|,|\n|$)",
            r"你是名为[\"“']?([^\"“”'。\n，,]{1,24})[\"”']?",
            r"身份.*?名为[\"“']?([^\"“”'。\n，,]{1,24})[\"”']?",
        ):
            for hit in re.findall(pattern, text):
                hit = str(hit).strip()
                if hit:
                    candidates.append(hit)
    out: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        cand = re.sub(r"^(?:一个|一位)", "", cand).strip()
        cand = cand.strip(" ：:,，。\"“”'")
        if not cand or cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
        # Also try suffix after leading digits/words so “16岁猫娘” → “猫娘”
        suffix = re.sub(r"^\d+[岁年月日]?", "", cand).strip()
        if suffix and suffix != cand and suffix not in seen:
            seen.add(suffix)
            out.append(suffix)
    return out


def _load_persona_by_declared_identity(persona: str, meta: dict[str, str], content: str) -> PersonaFile | None:
    for name in _persona_name_candidates(persona, meta, content):
        pf = load_persona(name)
        if pf:
            return pf
        target = _persona_match_target(name)
        if not target:
            continue
        for f in sorted(_PERSONAS_DIR.glob("*.md")):
            try:
                raw = f.read_text(encoding="utf-8")
            except Exception:
                continue
            file_meta, _ = _split_meta_content(raw)
            file_names = [
                file_meta.get("name", ""),
                f.stem,
                *(_mojibake_variants(file_meta.get("name", ""))),
                *(_mojibake_variants(f.stem)),
            ]
            if any(_persona_match_target(n) == target for n in file_names if n):
                return load_persona(f.stem)
    return None


def _matches_persona_content(target: str, raw: str, content: str) -> bool:
    if not target:
        return False
    target_tokens = _persona_ascii_tokens(target)
    for candidate in [raw, content, *_mojibake_variants(raw), *_mojibake_variants(content)]:
        normalized = _persona_match_target(candidate)
        if not normalized:
            continue
        if target == normalized:
            return True
        if len(target) >= 200 and target in normalized:
            return True
        if len(normalized) >= 200 and normalized in target:
            return True
        candidate_tokens = _persona_ascii_tokens(normalized)
        if target_tokens and len(target_tokens & candidate_tokens) >= 3:
            return True
    return False


def resolve_persona_file_by_content(persona: str) -> PersonaFile | None:
    """Find the current persona file matching stored DB content."""
    if not persona or not _PERSONAS_DIR.exists():
        return None
    meta, content = _split_meta_content(persona)
    pf = _load_persona_by_declared_identity(persona, meta, content)
    if pf:
        return pf

    target = _persona_match_target(content or persona)
    if not target:
        return None
    for f in sorted(_PERSONAS_DIR.glob("*.md")):
        try:
            raw = f.read_text(encoding="utf-8")
        except Exception:
            continue
        _, file_content = _split_meta_content(raw)
        if _matches_persona_content(target, raw, file_content):
            return load_persona(f.stem)
    return None


def find_persona_voice_sample_by_content(persona: str) -> Optional[Path]:
    """Resolve a voice sample from persona text, even when DB content omits metadata."""
    if not persona:
        return None
    meta, content = _split_meta_content(persona)
    for name in _persona_name_candidates(persona, meta, content):
        direct = find_persona_voice_sample(name)
        if direct:
            return direct
    pf = _load_persona_by_declared_identity(persona, meta, content)
    if pf:
        direct = find_persona_voice_sample(pf.meta.name) or find_persona_voice_sample(pf.meta.id)
        if direct:
            return direct

    if not _PERSONAS_DIR.exists():
        return None
    target = _persona_match_target(content or persona)
    if not target:
        return None
    for f in sorted(_PERSONAS_DIR.glob("*.md")):
        try:
            raw = f.read_text(encoding="utf-8")
        except Exception:
            continue
        file_meta, file_content = _split_meta_content(raw)
        if _matches_persona_content(target, raw, file_content):
            return find_persona_voice_sample(file_meta.get("name", "")) or find_persona_voice_sample(f.stem)
    return None


def _parse_voice_preference(value: str, default: float = 0.0) -> float:
    try:
        parsed = float((value or "").strip())
    except Exception:
        return default
    return max(0.0, min(1.0, parsed))


def persona_voice_preference_by_content(persona: str, default: float = 0.0) -> float:
    """Resolve voice_reply_preference from persona metadata or matching persona file."""
    if not persona:
        return default
    meta, content = _split_meta_content(persona)
    if "voice_reply_preference" in meta:
        return _parse_voice_preference(meta.get("voice_reply_preference", ""), default)

    if not _PERSONAS_DIR.exists():
        return default
    pf = _load_persona_by_declared_identity(persona, meta, content)
    if pf:
        raw = (_PERSONAS_DIR / pf.meta.filename).read_text(encoding="utf-8")
        file_meta, _ = _split_meta_content(raw)
        return _parse_voice_preference(file_meta.get("voice_reply_preference", ""), default)
    target = _persona_match_target(content or persona)
    if not target:
        return default
    for f in sorted(_PERSONAS_DIR.glob("*.md")):
        try:
            raw = f.read_text(encoding="utf-8")
        except Exception:
            continue
        file_meta, file_content = _split_meta_content(raw)
        if _matches_persona_content(target, raw, file_content):
            return _parse_voice_preference(file_meta.get("voice_reply_preference", ""), default)
    return default


def persona_intermediate_feedback_preference_by_content(persona: str, default: float = 0.5) -> float:
    """Resolve intermediate_feedback_preference from persona metadata or matching persona file."""
    if not persona:
        return default
    meta, content = _split_meta_content(persona)
    if "intermediate_feedback_preference" in meta:
        return _parse_voice_preference(meta.get("intermediate_feedback_preference", ""), default)

    if not _PERSONAS_DIR.exists():
        return default
    pf = _load_persona_by_declared_identity(persona, meta, content)
    if pf:
        raw = (_PERSONAS_DIR / pf.meta.filename).read_text(encoding="utf-8")
        file_meta, _ = _split_meta_content(raw)
        return _parse_voice_preference(file_meta.get("intermediate_feedback_preference", ""), default)
    target = _persona_match_target(content or persona)
    if not target:
        return default
    for f in sorted(_PERSONAS_DIR.glob("*.md")):
        try:
            raw = f.read_text(encoding="utf-8")
        except Exception:
            continue
        file_meta, file_content = _split_meta_content(raw)
        if _matches_persona_content(target, raw, file_content):
            return _parse_voice_preference(file_meta.get("intermediate_feedback_preference", ""), default)
    return default


def persona_voice_instruct_by_content(persona: str, default: str = "") -> str:
    """Resolve voice_instruct from persona metadata or matching persona file.

    Archive persona rows intentionally store only the body text, while voice
    metadata lives in personas/*.md frontmatter.  TTS paths therefore need the
    same body-to-file lookup used by voice_reply_preference.
    """
    if not persona:
        return default

    try:
        from app.core.orchestrator_utils import _filter_voice_instruct
    except Exception:
        def _filter_voice_instruct(raw: str) -> str:  # type: ignore[no-redef]
            return (raw or "").strip()

    meta, content = _split_meta_content(persona)
    if "voice_instruct" in meta:
        return _filter_voice_instruct(meta.get("voice_instruct", "")) or default

    if not _PERSONAS_DIR.exists():
        return default
    pf = _load_persona_by_declared_identity(persona, meta, content)
    if pf:
        raw = (_PERSONAS_DIR / pf.meta.filename).read_text(encoding="utf-8")
        file_meta, _ = _split_meta_content(raw)
        return _filter_voice_instruct(file_meta.get("voice_instruct", "")) or default
    target = _persona_match_target(content or persona)
    if not target:
        return default
    for f in sorted(_PERSONAS_DIR.glob("*.md")):
        try:
            raw = f.read_text(encoding="utf-8")
        except Exception:
            continue
        file_meta, file_content = _split_meta_content(raw)
        if _matches_persona_content(target, raw, file_content):
            return _filter_voice_instruct(file_meta.get("voice_instruct", "")) or default
    return default


def get_default_persona_content() -> str:
    return "你是一个友好、乐于助人的助理。说话自然，避免过度套话。"
