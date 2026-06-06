import os

from app.core.orchestrator_checks import (
    _add_mentioned_existing_deliverables,
    _autofix_deliverables,
)
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


def test_mentioned_autofix_adds_existing_audio_file(tmp_path):
    (tmp_path / "random_test_voice_final.wav").write_bytes(b"RIFF" + b"\0" * 220_000)
    (tmp_path / "helper_random_voice_final_generate_test_voice.py").write_text(
        "print('internal script')\n",
        encoding="utf-8",
    )

    plan = _plan_with_text(
        "已验证：`random_test_voice_final.wav` — WAV, PCM, 单声道, 22050Hz, 16位, 5秒。"
    )
    plan.deliverables = ["helper_random_voice_final_generate_test_voice.py"]

    _add_mentioned_existing_deliverables(
        plan,
        str(tmp_path),
        files_before=set(),
    )

    assert plan.deliverables == [
        "helper_random_voice_final_generate_test_voice.py",
        "random_test_voice_final.wav",
    ]


def test_autofix_prefers_new_audio_over_internal_python_temp(tmp_path):
    temp_dir = tmp_path / ".temp"
    temp_dir.mkdir()
    (tmp_path / "_py_cmd_5b1c4717.py").write_text("print('internal')\n", encoding="utf-8")
    (temp_dir / "random_test_voice_140833.wav").write_bytes(b"RIFF" + b"\0" * 128)

    plan = ResponsePlan(
        intent="生成随机测试语音文件",
        key_points=[],
        tone="plain",
        length_hint="short",
    )

    _autofix_deliverables(
        plan,
        user_message="生成",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
    )

    assert plan.deliverables == [".temp/random_test_voice_140833.wav"]


def test_autofix_skips_framework_contract_when_selecting_outputs(tmp_path):
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

    _autofix_deliverables(
        plan,
        user_message="输出报告文件",
        needs_tools=True,
        workspace_dir=str(tmp_path),
        files_before=set(),
    )

    assert plan.deliverables == ["compression_report.docx"]
