import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.workspace import inspect_file


def _write_minimal_docx(path: Path, texts: list[str], with_image: bool) -> None:
    paragraphs = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        for text in texts
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", document_xml)
        if with_image:
            zf.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\nfake")


def test_inspect_file_warns_when_docx_has_image_but_stale_chart_placeholder(tmp_path):
    _write_minimal_docx(
        tmp_path / "paper.docx",
        [
            "由于当前图表 PNG 尚未生成，本文不嵌入图片。",
            "6 性能图表预留章节",
        ],
        with_image=True,
    )

    result = inspect_file(str(tmp_path), "paper.docx")

    assert result["ok"] is True
    assert result["metadata"]["image_count"] == 1
    assert result["metadata"]["stale_chart_placeholder_hit"]
    assert any("已包含图片但正文仍有阶段性占位说法" in w for w in result["warnings"])


def test_inspect_file_does_not_warn_stale_chart_placeholder_without_images(tmp_path):
    _write_minimal_docx(
        tmp_path / "paper.docx",
        ["由于当前图表 PNG 尚未生成，本文不嵌入图片。"],
        with_image=False,
    )

    result = inspect_file(str(tmp_path), "paper.docx")

    assert result["ok"] is True
    assert result["metadata"]["image_count"] == 0
    assert not any("已包含图片但正文仍有阶段性占位说法" in w for w in result.get("warnings", []))


def test_inspect_file_reports_directory_with_actionable_guidance(tmp_path):
    (tmp_path / "_env" / "src").mkdir(parents=True)

    result = inspect_file(str(tmp_path), "_env")

    assert result["ok"] is False
    assert result["type"] == "directory"
    assert "Path is a directory" in result["error"]
    assert "File does not exist" not in result["error"]
    assert result["directory_entries"] == [{"name": "src", "type": "directory"}]
    assert result["entries_truncated"] is False
    assert "workspace list" in result["suggested_tools"]
