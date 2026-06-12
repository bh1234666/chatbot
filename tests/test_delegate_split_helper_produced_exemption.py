from __future__ import annotations

from app.llm.tools.delegate import (
    _deterministic_source_read_split_recommendations,
    _should_soften_source_read_split_for_single_text_output,
)


def test_split_recommended_when_no_explicit_input_files():
    """A typical paper-extraction job over many raw source files (only mentioned in prompt,
    not explicit input_files) should still trigger the split recommendation — that is the
    original failure mode P132 keeps in place."""
    task = {
        "task_id": "extract_paper_figs",
        "kind": "edit",
        "prompt": (
            "Read all of these source materials in the current directory and extract figures: "
            "report1.docx, report2.docx, report3.pdf, scan4.png, scan5.png, "
            "scan6.png, table7.xlsx, table8.xlsx, slide9.pptx, slide10.pptx."
        ),
        # No input_files key at all — main process did not enumerate.
    }
    recs = _deterministic_source_read_split_recommendations([task])
    assert len(recs) == 1
    assert recs[0]["task_id"] == "extract_paper_figs"
    assert recs[0]["observed_split_boundary_names"]


def test_no_split_when_input_files_explicit():
    """When the main process explicitly listed input_files, the split guard backs off and
    trusts the LLM + dispatch guards (P131) to route correctly. This unblocks docx-assembly
    over already-staged sibling-helper outputs (analyses / framework_contract / csv) which
    otherwise loops between split-block and P131-block."""
    task = {
        "task_id": "assemble_docx",
        "kind": "edit",
        "prompt": (
            "Read all of these inputs and assemble db_index_paper.docx:\n"
            "- _env/framework_contract.md\n"
            "- _env/analysis/rbt_analysis.md\n"
            "- _env/analysis/skiplist_analysis.md\n"
            "- _env/analysis/btree_analysis.md\n"
            "- _env/analysis/bplus_analysis.md\n"
            "- _env/analysis/hybrid_analysis.md\n"
            "- _env/bench_results/rbt.csv\n"
            "- _env/bench_results/skiplist.csv\n"
            "- _env/bench_results/btree.csv\n"
            "- _env/bench_results/bplus.csv\n"
            "- _env/bench_results/hybrid.csv\n"
        ),
        "input_files": [
            "_env/framework_contract.md",
            "_env/analysis/rbt_analysis.md",
            "_env/analysis/skiplist_analysis.md",
            "_env/analysis/btree_analysis.md",
            "_env/analysis/bplus_analysis.md",
            "_env/analysis/hybrid_analysis.md",
            "_env/bench_results/rbt.csv",
            "_env/bench_results/skiplist.csv",
            "_env/bench_results/btree.csv",
            "_env/bench_results/bplus.csv",
            "_env/bench_results/hybrid.csv",
        ],
    }
    recs = _deterministic_source_read_split_recommendations([task])
    assert recs == []


def test_no_split_for_explicit_input_files_even_when_all_raw_sources():
    """Same exemption applies even if every input is a raw user pdf/docx — explicit
    enumeration means the LLM has decided. P131 + LLM judgment do the rest."""
    task = {
        "task_id": "explicit_pdfs",
        "kind": "edit",
        "prompt": "Extract figures from these user PDFs: a.pdf b.pdf c.pdf d.pdf e.pdf f.pdf g.pdf",
        "input_files": ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf", "f.pdf", "g.pdf"],
    }
    recs = _deterministic_source_read_split_recommendations([task])
    assert recs == []


def test_source_read_split_softens_for_single_text_synthesis_with_small_fact_set():
    task = {
        "task_id": "small_report",
        "kind": "code",
        "prompt": (
            "Write summary.md from already confirmed profile.yaml and data.json facts. "
            "Budget, constraints, and accepted venue facts are listed inline below."
        ),
        "expected_outputs": ["summary.md"],
    }
    rec = {
        "task_id": "small_report",
        "observed_split_boundary_names": ["read_user_profile", "read_data_json", "summary_synthesis"],
        "reason": "Separate source-material reading from final writing.",
    }

    assert _should_soften_source_read_split_for_single_text_output(task, rec) is True


def test_source_read_split_does_not_soften_for_broad_raw_material_batch():
    files = "\n".join(f"- report{i}.docx" for i in range(10))
    task = {
        "task_id": "extract_reports",
        "kind": "code",
        "prompt": f"Read and extract source material from these files:\n{files}",
        "expected_outputs": ["summary.md"],
    }
    rec = {
        "task_id": "extract_reports",
        "observed_split_boundary_names": ["read_sources_batch_1", "read_sources_batch_2"],
        "reason": "Read helpers should split broad source material first.",
    }

    assert _should_soften_source_read_split_for_single_text_output(task, rec) is False
