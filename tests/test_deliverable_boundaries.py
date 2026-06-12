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


def test_prefix_resolution_does_not_pick_latest_candidate_symbolically():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")

    assert "workspace.prefix_resolve.ambiguous" in src
    assert "no automatic choice was made" in src
    assert "getmtime" not in src[src.find("if missing:"):src.find("if _resolved:")]
