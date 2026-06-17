import os

from app.core.orchestrator_checks import (
    _add_mentioned_existing_deliverables,
    _collect_deliverable_candidates,
)
from app.core.orchestrator_entry import _autofix_deliverables
from app.schemas.api import ResponsePlan


def _plan_with_text(text: str) -> ResponsePlan:
    return ResponsePlan(
        intent=text,
        key_points=[],
        tone="plain",
        length_hint="short",
    )


def test_mentioned_candidates_skip_uploaded_source_files(tmp_path):
    upload_dir = tmp_path / "uploaded_files"
    upload_dir.mkdir()
    (upload_dir / "finance_sample.csv").write_text("month,revenue\n2026-01,1\n", encoding="utf-8")
    (tmp_path / "sort_performance_report.docx").write_bytes(b"docx bytes")

    plan = _plan_with_text(
        "基于 finance_sample.csv 完成分析，已生成 sort_performance_report.docx。"
    )

    candidates = _add_mentioned_existing_deliverables(
        plan,
        str(tmp_path),
        files_before={os.path.join("uploaded_files", "finance_sample.csv")},
    )

    assert candidates == ["sort_performance_report.docx"]
    assert plan.deliverables == []


def test_mentioned_candidates_only_include_new_generated_files(tmp_path):
    (tmp_path / "old_report.docx").write_bytes(b"old")
    (tmp_path / "new_report.docx").write_bytes(b"new")

    plan = _plan_with_text("已生成 old_report.docx 和 new_report.docx。")

    candidates = _add_mentioned_existing_deliverables(
        plan,
        str(tmp_path),
        files_before={"old_report.docx"},
    )

    assert candidates == ["new_report.docx"]
    assert plan.deliverables == []


def test_mentioned_candidates_include_existing_audio_file(tmp_path):
    (tmp_path / "random_test_voice_final.wav").write_bytes(b"RIFF" + b"\0" * 220_000)
    (tmp_path / "helper_random_voice_final_generate_test_voice.py").write_text(
        "print('internal script')\n",
        encoding="utf-8",
    )

    plan = _plan_with_text(
        "已验证：`random_test_voice_final.wav` — WAV, PCM, 单声道, 22050Hz, 16位, 5秒。"
    )
    plan.deliverables = ["helper_random_voice_final_generate_test_voice.py"]

    candidates = _add_mentioned_existing_deliverables(
        plan,
        str(tmp_path),
        files_before=set(),
    )

    assert candidates == ["random_test_voice_final.wav"]
    assert plan.deliverables == [
        "helper_random_voice_final_generate_test_voice.py",
    ]


def test_mentioned_candidates_never_mutate_plan(tmp_path):
    (tmp_path / "new_report.docx").write_bytes(b"new")
    plan = _plan_with_text("已生成 new_report.docx。")

    candidates = _add_mentioned_existing_deliverables(
        plan,
        str(tmp_path),
        files_before=set(),
    )

    assert candidates == ["new_report.docx"]
    assert plan.deliverables == []


def test_delivery_candidates_prefer_new_audio_over_internal_python_temp(tmp_path):
    (tmp_path / "_py_cmd_5b1c4717.py").write_text("print('internal')\n", encoding="utf-8")
    (tmp_path / "random_test_voice_140833.wav").write_bytes(b"RIFF" + b"\0" * 128)

    plan = ResponsePlan(
        intent="生成随机测试语音文件",
        key_points=[],
        tone="plain",
        length_hint="short",
    )

    candidates = _collect_deliverable_candidates(
        plan,
        user_message="生成",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
    )

    assert candidates == ["random_test_voice_140833.wav"]
    assert plan.deliverables == []

    second_plan = ResponsePlan(
        intent="生成随机测试语音文件",
        key_points=[],
        tone="plain",
        length_hint="short",
    )
    _collect_deliverable_candidates(
        second_plan,
        user_message="生成",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
    )

    assert second_plan.deliverables == []


def test_entry_autofix_applies_candidates_to_plan(tmp_path):
    (tmp_path / "final_report.docx").write_bytes(b"docx bytes")
    plan = ResponsePlan(
        intent="生成报告",
        key_points=[],
        tone="plain",
        length_hint="short",
    )

    _autofix_deliverables(
        plan,
        user_message="输出报告文件",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
    )

    assert plan.deliverables == ["final_report.docx"]


def test_delivery_candidates_do_not_scan_session_temp_from_main_workspace(tmp_path):
    temp_dir = tmp_path / ".temp" / "_sessions" / "s_other_user"
    temp_dir.mkdir(parents=True)
    (temp_dir / "random_test_voice_140833.wav").write_bytes(b"RIFF" + b"\0" * 128)

    plan = ResponsePlan(
        intent="查看网页",
        key_points=[],
        tone="plain",
        length_hint="short",
    )

    candidates = _collect_deliverable_candidates(
        plan,
        user_message="查看这个网页",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
    )

    assert candidates == []
    assert plan.deliverables == []


def test_delivery_candidates_skip_framework_contract_when_selecting_outputs(tmp_path):
    (tmp_path / "framework_contract_ch6_v2.md").write_text(
        "internal helper framework\n",
        encoding="utf-8",
    )
    (tmp_path / "compression_report.docx").write_bytes(b"docx bytes")

    plan = ResponsePlan(
        intent="生成压缩算法报告",
        key_points=[],
        tone="plain",
        length_hint="short",
    )

    candidates = _collect_deliverable_candidates(
        plan,
        user_message="输出报告文件",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
    )

    assert candidates == ["compression_report.docx"]
    assert plan.deliverables == []
