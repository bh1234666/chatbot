"""P135: blocking quality issues are restricted to physical/unrecoverable failures.

User-special-case scenarios (template CSV, cover-only docx, single-line stub code,
placeholder image, brief acknowledgement document) used to be treated as blocking
and force the orchestrator into resume loops. With P135, those subjective size
thresholds are warnings, and the LLM decides whether the artifact matches the
user's actual intent.

Hard-blocked issues (the only remaining set):
  - text_mojibake_suspected: encoding damage in helper-visible text
  - png_invalid_header / jpg_invalid_header: corrupt image file
  - docx_table_cell_object_literal: serialized JSON in a cell
  - requires_main_resource: helper explicitly requested a resource
  - stat_failed: filesystem error
"""
from __future__ import annotations

from app.llm.tools.delegate_quality import (
    _BLOCKING_QUALITY_ISSUES,
    blocking_quality_warnings,
    document_structure_quantity_warnings,
    source_data_approximation_warnings,
)


def test_subjective_size_thresholds_are_warnings_not_blocking():
    """Document warnings tied to user-can-want-this-shape sizes must not block."""
    user_special_case_warnings = [
        # User asked for a template CSV / single-row sample / config-only csv
        {"file": "template.csv", "issue": "data_file_empty"},
        {"file": "header_only.csv", "issue": "data_file_no_rows"},
        # User wants a cover-only docx, single-line acknowledgement, brief memo
        {"file": "cover.docx", "issue": "document_too_small"},
        {"file": "memo.docx", "issue": "docx_too_few_paragraphs"},
        {"file": "brief.docx", "issue": "docx_too_few_chars"},
        # Placeholder/icon image, intentional 1x1 transparent PNG
        {"file": "spacer.png", "issue": "image_too_small"},
        # Tiny stub script, entry point file, or sentinel module
        {"file": "stub.py", "issue": "code_file_too_small"},
        # Multi-file delivery with brief report (LLM may have summarized correctly)
        {"file": "<helper-report>", "issue": "suspicious_short_completion"},
        # Benchmark schema/timing checks (statistical, often false-positive)
        {"file": "results.csv", "issue": "benchmark_schema_missing_columns"},
        {"file": "results.csv", "issue": "benchmark_timing_precision_loss"},
        {"file": "results.csv", "issue": "benchmark_complexity_anomaly"},
        {"file": "results.csv", "issue": "benchmark_unfair_ratio"},
        {"file": "results.csv", "issue": "csv_column_mismatch"},
        # Document content judgments (depend on prompt parsing)
        {"file": "paper.docx", "issue": "document_internal_source_label"},
        {"file": "slides.pptx", "issue": "pptx_expected_slide_order_mismatch"},
        {"file": "paper.docx", "issue": "document_expected_text_missing"},
        {"file": "paper.docx", "issue": "academic_citation_unverified"},
        {"file": "paper.docx", "issue": "docx_table_too_wide"},
        {"file": "paper.docx", "issue": "document_unbacked_pass_standard"},
        {"file": "paper.docx", "issue": "document_source_data_approximated_from_truncation"},
        {"file": "paper.docx", "issue": "document_required_table_count_shortfall"},
        {"file": "paper.docx", "issue": "document_required_figure_count_shortfall"},
    ]
    blocking = blocking_quality_warnings(user_special_case_warnings)
    assert blocking == [], (
        f"these issues should be warnings only, but they were treated as blocking: "
        f"{[w.get('issue') for w in blocking]}"
    )


def test_hard_physical_failures_remain_blocking():
    """The reduced blocking set still catches truly unrecoverable failures."""
    physical_failures = [
        {"file": "report.txt", "issue": "text_mojibake_suspected"},
        {"file": "scan.png", "issue": "png_invalid_header"},
        {"file": "photo.jpg", "issue": "jpg_invalid_header"},
        {"file": "table.docx", "issue": "docx_table_cell_object_literal"},
        {"file": "<helper>", "issue": "requires_main_resource"},
        {"file": "out.csv", "issue": "stat_failed"},
    ]
    blocking = blocking_quality_warnings(physical_failures)
    assert {w.get("issue") for w in blocking} == {
        "text_mojibake_suspected",
        "png_invalid_header",
        "jpg_invalid_header",
        "docx_table_cell_object_literal",
        "requires_main_resource",
        "stat_failed",
    }


def test_explicit_blocking_severity_still_honored():
    """If a check explicitly tags severity='blocking', it still blocks regardless of issue name."""
    explicit = [{"file": "x", "issue": "data_file_empty", "severity": "blocking"}]
    blocking = blocking_quality_warnings(explicit)
    assert len(blocking) == 1
    assert blocking[0]["issue"] == "data_file_empty"


def test_blocking_set_intentionally_minimal():
    """Pin the exact set so future additions are deliberate."""
    assert _BLOCKING_QUALITY_ISSUES == {
        "docx_table_cell_object_literal",
        "png_invalid_header",
        "jpg_invalid_header",
        "requires_main_resource",
        "stat_failed",
        "text_mojibake_suspected",
    }


def test_source_data_approximation_from_truncation_is_model_visible_warning():
    report = (
        "Missing or warnings: RBT scaling values at 10K/100K: Approximated from O(log n) "
        "scaling since only the n=1,000 row data was fully extracted from the truncated "
        "read output. Actual CSV values should be spot-checked if precision is required."
    )

    warnings = source_data_approximation_warnings(report)

    assert len(warnings) == 1
    assert warnings[0]["issue"] == "document_source_data_approximated_from_truncation"
    assert warnings[0]["severity"] == "warning"
    assert "truncated" in warnings[0]["excerpt"].lower()
    assert blocking_quality_warnings(warnings) == []


def test_document_structure_quantity_shortfalls_are_model_visible_warnings():
    prompt = (
        "Final DOCX Assembly Contract. At least 4 comparative tables are required. "
        "At minimum: 3 figures or charts must be present."
    )

    warnings = document_structure_quantity_warnings(
        prompt,
        file="paper.docx",
        table_count=2,
        image_count=0,
    )

    issues = {warning["issue"] for warning in warnings}
    assert issues == {
        "document_required_table_count_shortfall",
        "document_required_figure_count_shortfall",
    }
    assert all(warning["severity"] == "warning" for warning in warnings)
    assert blocking_quality_warnings(warnings) == []
