import pytest

from app.core.orchestrator_utils import (
    _clean_deliverable_filenames,
    _is_internal_deliverable_file,
)


def test_internal_deliverable_boundary_blocks_helper_evidence():
    internal_names = [
        "_session_manifest.json",
        ".helper_arch_full_report.txt",
        "helper_file_sum_top6__py_cmd_ab12cd34.py",
        "helper_file_sum_next6_report.txt",
        "file_sum_top6__analysis_output.txt",
        "file_sum_top6_analyse2_out.txt",
        "file_sum_top6_analyse_full.txt",
        "_delegate_task/output.txt",
        "_helpers_shared/report.txt",
        "_env/app/core/orchestrator.py",
        "_py_cmd_ab12cd34.py",
        ".secret/report.txt",
        "ocr_result_page1.txt",
        "framework_contract_ch6_v2.md",
        "shared_framework_contract.json",
        "amsc_framework_contract.txt",
    ]

    assert all(_is_internal_deliverable_file(name) for name in internal_names)


def test_internal_deliverable_boundary_allows_user_artifacts():
    visible_names = [
        "analysis_report.md",
        "architecture_review.txt",
        "data_summary.csv",
        "chart.png",
        ".temp/random_test_voice_140833.wav",
        "src/algolab/graph.py",
        "reports/file_sum_top6.md",
    ]

    assert not any(_is_internal_deliverable_file(name) for name in visible_names)


def test_clean_deliverables_normalizes_without_overriding_model_choice():
    cleaned = _clean_deliverable_filenames([
        "analysis_report.md — final report",
        "_session_manifest.json",
        "helper_file_sum_top6__py_cmd_ab12cd34.py",
        "file_sum_top6__analysis_output.txt",
        ".temp/random_test_voice_140833.wav",
        "chart.png",
    ])

    assert cleaned == [
        "analysis_report.md",
        "_session_manifest.json",
        "helper_file_sum_top6__py_cmd_ab12cd34.py",
        "file_sum_top6__analysis_output.txt",
        ".temp/random_test_voice_140833.wav",
        "chart.png",
    ]


def test_deliverable_warning_review_treats_plan_selection_as_evidence():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")

    assert "`current_deliverables` and `plan_*` fields are also review inputs" in src
    assert "old assistant messages or broad workspace listings" in src
    assert "all selected file(s) already existed before this round" in src
    assert "valid re-delivery only when the current user request explicitly asks" in src


def test_deliverable_warning_facts_are_not_raw_round3_key_points():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")
    block = src[
        src.find("if _deliverable_warning_facts:"):
        src.find("want = set(plan.deliverables) if plan.deliverables else set()", src.find("if _deliverable_warning_facts:"))
    ]

    assert "warning_facts=_deliverable_warning_facts" in block
    assert "for _fact in _deliverable_warning_facts" not in block
    assert "avoid exposing internal file names or paths" in block
    assert "keeping explicit plan.deliverables" not in src
    assert "keeping explicit voice_reply_file" not in src


def test_prefix_resolution_does_not_pick_latest_candidate_symbolically():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")

    assert "workspace.prefix_resolve.ambiguous" in src
    assert "no automatic choice was made" in src
    assert "getmtime" not in src[src.find("if missing:"):src.find("if _resolved:")]


def test_round3_visible_file_labels_hide_internal_artifacts():
    from app.core.orchestrator_entry import _round3_visible_file_label

    assert (
        _round3_visible_file_label("_voice_e1cbf550_1781276502334.wav", role="voice reply file")
        == "[voice reply file]"
    )
    assert _round3_visible_file_label(".helper_arch_full_report.txt") == "[file]"
    assert _round3_visible_file_label("_helpers_shared/report.txt", role="candidate file") == "[candidate file]"
    assert _round3_visible_file_label("analysis_report.md") == "analysis_report.md"
    assert (
        _round3_visible_file_label(
            "taskabc_result.docx",
            known_task_id_prefixes=["taskabc"],
        )
        == "result.docx"
    )
    assert (
        _round3_visible_file_label(
            "taskabc_result.docx",
            displayed_remap={"taskabc_result.docx": "result.docx"},
        )
        == "result.docx"
    )


def test_round3_sanitizer_rewrites_internal_file_mentions_without_changing_visible_files():
    from app.core.orchestrator_entry import _round3_sanitize_file_mentions

    text = (
        "helper reported _voice_e1cbf550_1781276502334.wav and "
        ".helper_arch_full_report.txt; final artifact taskabc_report.docx is ready."
    )

    cleaned = _round3_sanitize_file_mentions(
        text,
        file_names={
            "_voice_e1cbf550_1781276502334.wav",
            ".helper_arch_full_report.txt",
            "taskabc_report.docx",
        },
        known_task_id_prefixes=["taskabc"],
    )

    assert "_voice_e1cbf550_1781276502334.wav" not in cleaned
    assert ".helper_arch_full_report.txt" not in cleaned
    assert "helper reported" not in cleaned
    assert "[voice reply file]" in cleaned
    assert "[file]" in cleaned
    assert "report.docx" in cleaned


def test_round3_sanitizer_rewrites_internal_workflow_terms():
    from app.core.orchestrator_entry import (
        _looks_like_user_visible_protocol_text,
        _round3_sanitize_file_mentions,
        _sanitize_user_visible_internal_terms,
    )

    text = (
        "Round2 helper returned _helpers_shared/read/page.txt; "
        "then env_run checked _env/app.py and delegate task finished. "
        "I copied from internal_shared/fetch/page.txt; internal_run_123 produced result; "
        "processing_records=ok."
    )

    assert _looks_like_user_visible_protocol_text(text)
    assert _looks_like_user_visible_protocol_text("internal_shared/fetch/page.txt is ready")
    assert _looks_like_user_visible_protocol_text("internal_run_123 produced result")
    assert _looks_like_user_visible_protocol_text("processing_records=ok")
    assert _looks_like_user_visible_protocol_text("TTS returned persona_guard_refused_tts resource_required")
    assert _looks_like_user_visible_protocol_text("工具链生成过程卡在精准执行的规则")

    cleaned = _sanitize_user_visible_internal_terms(text)
    sanitized = _round3_sanitize_file_mentions(text)
    combined = cleaned + "\n" + sanitized

    for term in (
        "Round2", "helper", "_helpers_shared", "internal_shared", "env_run",
        "_env/", "delegate task", "internal_run", "processing_records",
        "persona_guard", "resource_required", "工具链", "精准执行的规则",
    ):
        assert term not in combined
    assert "处理" in combined or "项目" in combined


def test_round3_visible_plan_copy_hides_stale_voice_artifact_without_mutating_plan():
    from app.core.orchestrator_entry import _round3_visible_plan_and_files
    from app.schemas.api import ResponsePlan

    stale_voice = "_voice_e1cbf550_1781276502334.wav"
    plan = ResponsePlan(
        intent=f"Inspect the requested web page; do not send {stale_voice}.",
        key_points=[f"The current task is page inspection; {stale_voice} is old context."],
        tone="plain",
        length_hint="short",
        callbacks=[f"Do not expose helper state or {stale_voice}."],
        avoid=[f"Do not present {stale_voice} as the current artifact."],
        deliverables=[stale_voice],
        delivery_partial=[stale_voice],
    )

    visible_plan, visible_files = _round3_visible_plan_and_files(
        plan,
        files=[(stale_voice, "/v1/chat/files/archive/group/_voice_e1cbf550_1781276502334.wav")],
    )

    visible_text = str(visible_plan.model_dump()) + str([name for name, _url in (visible_files or [])])
    assert stale_voice not in visible_text
    assert "[voice reply file]" in visible_text
    assert visible_plan.deliverables == ["[file]"]
    assert visible_plan.delivery_partial == ["[missing file]"]
    assert visible_files == [
        ("[file]", "/v1/chat/files/archive/group/_voice_e1cbf550_1781276502334.wav")
    ]
    assert plan.deliverables == [stale_voice]
    assert plan.delivery_partial == [stale_voice]


def test_round2_voice_handoff_key_point_uses_round3_safe_label():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")
    block = src[
        src.find("async def _prepare_round2_voice_handoff_before_delivery"):
        src.find("def _existing_environment_project_files")
    ]

    assert "_round3_visible_file_label(_vf_base, role=\"voice reply file\")" in block
    assert "preflight owns authorization" in block
    assert "if _allow_vf:" not in block
    assert "已生成语音回复已通过本轮复核" not in block
    assert "按 Round2 指定,最终回复使用已生成语音" not in block
    assert ":{_vf_base}" not in block


@pytest.mark.asyncio
async def test_delivery_review_can_drop_preexisting_audio_for_unrelated_request(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "_voice_e1cbf550_1781276502334.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Inspect the official web page.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )
    captured = {}

    async def fake_chat_json(messages, **kwargs):
        captured["messages"] = messages
        return {
            "deliverables": [],
            "reason": f"The current request asks to inspect a page; {fname} is a pre-existing historical file.",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="Please inspect this official web page.",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected file(s) already existed before this round: "
            f"{fname}. This can be a valid re-delivery only when the current user request explicitly asks."
        ],
    )

    assert plan.deliverables == []
    decision_facts = [
        str(item) for item in plan.key_points
        if "selected deliverables were revised" in str(item)
    ]
    assert decision_facts
    assert fname not in decision_facts[-1]
    payload = str(captured["messages"])
    assert "current_deliverables" in payload
    assert fname in payload
    assert "already existed before this round" in payload


@pytest.mark.asyncio
async def test_delivery_review_can_keep_preexisting_audio_when_current_request_asks(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "random_test_voice_final.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Resend the requested audio file.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )
    captured = {}

    async def fake_chat_json(messages, **kwargs):
        captured["messages"] = messages
        return {
            "deliverables": [fname],
            "reason": "The current request explicitly asks to resend the existing audio file.",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="请把刚才那个测试语音文件重新发我",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected audio file(s) already existed before this round: "
            f"{fname}. This can be a valid re-delivery only when the current user request explicitly asks."
        ],
    )

    assert plan.deliverables == [fname]
    assert any(
        "selected deliverables remain" in str(item)
        and fname in str(item)
        for item in plan.key_points
    )
    payload = str(captured["messages"])
    assert '"extension": ".wav"' in payload
    assert '"preexisting_at_round_start": true' in payload
    assert "重新发" in payload


@pytest.mark.asyncio
async def test_delivery_review_sanitizes_kept_system_voice_filename_in_round3_facts(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "_voice_e1cbf550_1781276502334.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Resend the requested audio file.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )

    async def fake_chat_json(messages, **kwargs):
        return {
            "deliverables": [fname],
            "reason": f"The current request explicitly asks to resend {fname}.",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="请重发刚才那条语音",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected audio file(s) already existed before this round: "
            f"{fname}. This can be a valid re-delivery only when the current user request explicitly asks."
        ],
    )

    assert plan.deliverables == [fname]
    assert plan.key_points
    assert fname not in "\n".join(str(item) for item in plan.key_points)
    assert "[reviewed file]" in plan.key_points[-1]


def test_round2_voice_handoff_has_no_post_helper_llm_review():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")
    handoff_block = src[
        src.find("async def _prepare_round2_voice_handoff_before_delivery"):
        src.find("def _existing_environment_project_files", src.find("async def _prepare_round2_voice_handoff_before_delivery"))
    ]

    assert "_review_round2_voice_reply_file_candidate" not in src
    assert "json.voice_reply_file_review" not in src
    assert "voice.round2_handoff.review" not in src
    assert "_persona_voice_reply_guard" not in Path("app/core/orchestrator.py").read_text(encoding="utf-8")
    assert "json.persona_voice_guard" not in Path("app/core/orchestrator.py").read_text(encoding="utf-8")
    assert "chat_json(" not in handoff_block
    assert "model_pool" not in handoff_block
    assert "_persona_voice_reply_guard" not in handoff_block


@pytest.mark.asyncio
async def test_voice_reply_handoff_keeps_model_selected_preexisting_file_as_voice_only(tmp_path):
    from app.core import orchestrator_entry
    from app.schemas.api import ResponsePlan

    fname = "voice_reply_catgirl.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Try voice reply.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
        voice_reply_file=fname,
    )

    voice_file, voice_text = await orchestrator_entry._prepare_round2_voice_handoff_before_delivery(
        plan,
        persona="persona",
        user_message="生成一个新的语音文件",
        workspace_dir=str(tmp_path),
        files_before={fname},
        list_workspace_files=lambda _workspace: [fname],
    )

    assert voice_file == fname
    assert voice_text == ""
    assert plan.voice_reply_file == fname
    assert plan.deliverables == []


@pytest.mark.asyncio
async def test_voice_reply_handoff_prepared_before_delivery_keeps_as_voice_only(tmp_path):
    from app.core import orchestrator_entry
    from app.schemas.api import ResponsePlan

    fname = "fresh_voice_reply.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Generate current voice reply.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
        voice_reply_file=fname,
    )

    voice_file, voice_text = await orchestrator_entry._prepare_round2_voice_handoff_before_delivery(
        plan,
        persona="persona",
        user_message="语音回复",
        workspace_dir=str(tmp_path),
        files_before=set(),
        list_workspace_files=lambda _workspace: [fname],
    )

    assert voice_file == fname
    assert voice_text == ""
    assert plan.voice_reply_file == fname
    assert plan.deliverables == []
    assert "最终回复使用该语音" in "\n".join(plan.key_points)


@pytest.mark.asyncio
async def test_voice_reply_handoff_does_not_run_post_tts_llm_review_or_guard(tmp_path):
    from app.core import orchestrator_entry
    from app.schemas.api import ResponsePlan

    fname = "fresh_voice_reply.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Generate current voice reply.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
        voice_reply_file=fname,
    )

    assert not hasattr(orchestrator_entry, "_review_round2_voice_reply_file_candidate")
    assert not hasattr(orchestrator_entry, "_persona_voice_reply_guard")

    voice_file, voice_text = await orchestrator_entry._prepare_round2_voice_handoff_before_delivery(
        plan,
        persona="persona",
        user_message="语音回复",
        workspace_dir=str(tmp_path),
        files_before=set(),
        list_workspace_files=lambda _workspace: [fname],
    )

    assert voice_file == fname
    assert voice_text == ""
    assert plan.deliverables == []


@pytest.mark.asyncio
async def test_voice_reply_handoff_uses_text_after_round2_clears_voice_file(tmp_path):
    from app.core import orchestrator_entry
    from app.schemas.api import ResponsePlan

    fname = "old_voice_reply.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Try voice reply.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
        voice_reply_file="",
        voice_reply_text="喵，收到啦。",
    )

    voice_file, voice_text = await orchestrator_entry._prepare_round2_voice_handoff_before_delivery(
        plan,
        persona="persona",
        user_message="生成一个新的语音文件",
        workspace_dir=str(tmp_path),
        files_before={fname},
        list_workspace_files=lambda _workspace: [fname],
    )

    assert voice_file == ""
    assert voice_text == "喵，收到啦。"
    assert plan.voice_reply_file == ""
    assert plan.deliverables == [fname]


@pytest.mark.asyncio
async def test_delivery_review_can_keep_preexisting_file_when_current_request_reuses_it(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "compression_report.docx"
    (tmp_path / fname).write_bytes(b"PK\x03\x04report")
    plan = ResponsePlan(
        intent="Resend the existing report.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )

    async def fake_chat_json(messages, **kwargs):
        return {
            "deliverables": [fname],
            "reason": "The current request explicitly asks to resend the existing report.",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="Resend the report from last time.",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected file(s) already existed before this round: "
            f"{fname}. This can be a valid re-delivery only when the current user request explicitly asks."
        ],
    )

    assert plan.deliverables == [fname]


@pytest.mark.asyncio
async def test_delivery_review_failure_demotes_warning_deliverables(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "_voice_e1cbf550_1781276502334.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Inspect the official web page.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )

    async def failing_chat_json(messages, **kwargs):
        raise TimeoutError("review timeout")

    monkeypatch.setattr(model_pool, "chat_json", failing_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="Please inspect this official web page.",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected file(s) already existed before this round: "
            f"{fname}. This can be valid only when the current request asks for it."
        ],
    )

    assert plan.deliverables == []
    key_points = "\n".join(str(item) for item in plan.key_points)
    assert "review was unavailable" in key_points
    assert "not attached automatically" in key_points
    assert fname not in key_points
    assert "[candidate file]" in key_points
