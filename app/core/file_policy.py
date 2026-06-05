from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024

SAFE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp",
    ".csv", ".json", ".xml", ".yaml", ".yml", ".toml",
    ".html", ".htm", ".css", ".js", ".md", ".txt", ".log",
    ".pdf", ".docx", ".pptx", ".xlsx",
    ".zip", ".tar", ".gz", ".7z",
    ".mp3", ".wav", ".ogg", ".mp4", ".webm",
    ".py", ".ipynb",
    ".c", ".h", ".cpp", ".hpp", ".cxx", ".hxx",
}

BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".out", ".o", ".obj",
    ".bat", ".cmd", ".ps1", ".sh",
    ".msi", ".com", ".scr",
    ".jar", ".apk", ".dmg", ".deb", ".rpm",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg"}


@dataclass(frozen=True)
class FileDeliveryDecision:
    allowed: bool
    reason: str = ""
    status_code: int = 200
    delivery_kind: str = "file"
    extension: str = ""


def classify_file_for_delivery(name: str, path: str | None = None, mime: str | None = None) -> FileDeliveryDecision:
    ext = Path(name or path or "").suffix.lower()
    if ext in BLOCKED_EXTENSIONS:
        return FileDeliveryDecision(
            allowed=False,
            reason=f"executable files are not downloadable: {ext}",
            status_code=403,
            extension=ext,
        )
    if ext not in SAFE_EXTENSIONS:
        return FileDeliveryDecision(
            allowed=False,
            reason=f"extension not allowed: {ext}",
            status_code=400,
            extension=ext,
        )
    if ext in IMAGE_EXTENSIONS or (mime or "").startswith("image/"):
        kind = "image"
    elif ext in AUDIO_EXTENSIONS or (mime or "").startswith("audio/"):
        kind = "voice"
    else:
        kind = "file"
    return FileDeliveryDecision(allowed=True, delivery_kind=kind, extension=ext)
