import os

from app.core.orchestrator_checks import _add_mentioned_existing_deliverables
from app.schemas.api import ResponsePlan


def _plan_with_text(text: str) -> ResponsePlan:
    return ResponsePlan(
        intent=text,
        key_points=[],
        tone="plain",
        length_hint="short",
    )


def test_mentioned_autofix_skips_uploaded_source_files(tmp_path):
    upload_dir = tmp_path / "uploaded_files"
    upload_dir.mkdir()
    (upload_dir / "finance_sample.csv").write_text("month,revenue\n2026-01,1\n", encoding="utf-8")
    (tmp_path / "sort_performance_report.docx").write_bytes(b"docx bytes")

    plan = _plan_with_text(
        "基于 finance_sample.csv 完成分析，已生成 sort_performance_report.docx。"
    )

    _add_mentioned_existing_deliverables(
        plan,
        str(tmp_path),
        files_before={os.path.join("uploaded_files", "finance_sample.csv")},
    )

    assert plan.deliverables == ["sort_performance_report.docx"]


def test_mentioned_autofix_only_adds_new_generated_files(tmp_path):
    (tmp_path / "old_report.docx").write_bytes(b"old")
    (tmp_path / "new_report.docx").write_bytes(b"new")

    plan = _plan_with_text("已生成 old_report.docx 和 new_report.docx。")

    _add_mentioned_existing_deliverables(
        plan,
        str(tmp_path),
        files_before={"old_report.docx"},
    )

    assert plan.deliverables == ["new_report.docx"]
