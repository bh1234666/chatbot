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
        "ocr_result_page1.txt",
    ]

    assert all(_is_internal_deliverable_file(name) for name in internal_names)


def test_internal_deliverable_boundary_allows_user_artifacts():
    visible_names = [
        "analysis_report.md",
        "architecture_review.txt",
        "data_summary.csv",
        "chart.png",
        "src/algolab/graph.py",
        "reports/file_sum_top6.md",
    ]

    assert not any(_is_internal_deliverable_file(name) for name in visible_names)


def test_clean_deliverables_filters_internal_but_keeps_reports():
    cleaned = _clean_deliverable_filenames([
        "analysis_report.md — final report",
        "_session_manifest.json",
        "helper_file_sum_top6__py_cmd_ab12cd34.py",
        "file_sum_top6__analysis_output.txt",
        "chart.png",
    ])

    assert cleaned == ["analysis_report.md", "chart.png"]
