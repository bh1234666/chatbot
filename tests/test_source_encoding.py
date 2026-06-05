from pathlib import Path


# Keep this file ASCII-only. These samples are intentionally built from
# Unicode escapes so the test itself cannot introduce mojibake into the tree.
MOJIBAKE_MARKERS = (
    "\ufffd",  # replacement character
    "\u00c3",  # UTF-8 decoded as Latin-1, e.g. C3 A4 -> U+00C3 U+00A4
    "\u00c2\u00b7",
    "\u00e2\u20ac",  # common start of curly quote / dash mojibake
    "\u934f\ue21b\u6a3f",  # one observed GBK/UTF-8 double-decode fragment
    "\u9359\ue21b",  # observed private-use mojibake fragment
    "\u8292\u9207",  # another common Chinese display mojibake fragment
    "\u935b\u6212\u8151",  # observed short GBK/UTF-8 comment fragment
)

GBK_MOJIBAKE_SOURCE_PHRASES = (
    "\u7684\u8bed\u97f3",
    "\u56de\u5f52\u6d4b\u8bd5",
    "\u6587\u4ef6",
    "\u4e00\u6bb5",
    "\u542f\u52a8",
    "\u670d\u52a1",
    "\u8fdb\u7a0b",
    "\u5b9e\u65f6",
    "\u8bed\u97f3",
    "\u56de\u590d",
    "\u65e5\u5fd7",
)


def _gbk_mojibake(text: str) -> str:
    return text.encode("utf-8").decode("gbk", errors="ignore")


GBK_MOJIBAKE_MARKERS = tuple(
    marker
    for marker in (_gbk_mojibake(phrase) for phrase in GBK_MOJIBAKE_SOURCE_PHRASES)
    if len(marker) >= 2
)


def test_runtime_sources_do_not_contain_mojibake_markers():
    root = Path(__file__).resolve().parents[1]
    self_path = Path(__file__).resolve()
    roots = [
        root / "app",
        root / "tests",
        root / "group_sim",
        root / "stress_tools",
    ]
    suffixes = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml"}
    excluded_parts = {".pytest_cache", "__pycache__", "runs"}

    offenders: list[str] = []
    for scan_root in roots:
        for path in scan_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if path.resolve() == self_path:
                continue
            if excluded_parts.intersection(path.parts):
                continue
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                rel = path.relative_to(root)
                offenders.append(f"{rel}:0: UTF-8 BOM is not allowed")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                rel = path.relative_to(root)
                offenders.append(f"{rel}:0: UTF-8 decode failed: {exc}")
                continue

            for lineno, line in enumerate(text.splitlines(), 1):
                has_private_use = any("\ue000" <= ch <= "\uf8ff" for ch in line)
                if (
                    has_private_use
                    or any(marker in line for marker in MOJIBAKE_MARKERS)
                    or any(marker in line for marker in GBK_MOJIBAKE_MARKERS)
                ):
                    rel = path.relative_to(root)
                    offenders.append(f"{rel}:{lineno}: {line[:120]}")

    assert offenders == []
