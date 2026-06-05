import asyncio

from app.llm.client_tools_loop import _retryable_delegate_facts_from_result
from app.llm.tools.delegate import _sanitize_and_validate_tasks


def test_readonly_helper_expected_outputs_are_cleared(tmp_path):
    args = {"tasks": [{
        "task_id": "map",
        "kind": "project_map",
        "prompt": "Read these files and report compact findings. Do not modify files.",
        "expected_outputs": ["_helper_report_top3.md"],
    }]}

    raw = asyncio.run(_sanitize_and_validate_tasks(
        args,
        main_workspace=str(tmp_path),
        archive_id="arch",
        group_id="group",
        user_id="user",
    ))

    assert raw[0]["kind"] == "project_map"
    assert raw[0]["expected_outputs"] == []


def test_read_helper_staged_evidence_output_becomes_internal(tmp_path):
    args = {"tasks": [{
        "task_id": "read_docx",
        "kind": "read",
        "prompt": (
            "Read staged project sources and save source evidence as "
            "_env/read_evidence_docx.txt."
        ),
        "expected_outputs": ["_env/read_evidence_docx.txt"],
        "acceptance_checks": ["_env/read_evidence_docx.txt exists and is nonempty"],
    }]}

    raw = asyncio.run(_sanitize_and_validate_tasks(
        args,
        main_workspace=str(tmp_path),
        archive_id="arch",
        group_id="group",
        user_id="user",
    ))

    assert raw[0]["kind"] == "read"
    assert raw[0]["expected_outputs"] == ["read_evidence_docx.txt"]
    assert raw[0]["write_scopes"] == []
    assert raw[0]["acceptance_checks"] == ["read_evidence_docx.txt exists and is nonempty"]
    assert "Write the final evidence text at the helper sandbox root" in raw[0]["prompt"]
    assert "save source evidence as read_evidence_docx.txt" in raw[0]["prompt"]


def test_readonly_architecture_review_is_not_rewritten_without_llm(tmp_path):
    args = {"tasks": [{
        "task_id": "arch_map",
        "kind": "verify",
        "prompt": (
            "Perform a READ-ONLY architecture review of this app backup. "
            "Identify split candidates and split risks from real project file evidence. Do not modify files."
        ),
    }]}

    raw = asyncio.run(_sanitize_and_validate_tasks(
        args,
        main_workspace=str(tmp_path),
        archive_id="arch",
        group_id="group",
        user_id="user",
    ))

    assert raw[0]["kind"] == "verify"
    assert raw[0]["expected_outputs"] == []


async def test_delegate_guard_accepts_project_analysis_kinds(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.tools import delegate

    captured = {}

    async def fake_chat_json(messages, *args, **kwargs):
        captured["system"] = messages[0]["content"]
        return {
            "should_act": True,
            "reason": "project analysis is correctly scoped",
            "split_recommendations": [],
            "kind_recommendations": [
                {
                    "task_id": "arch_client_loop_py",
                    "current_kind": "file_summary",
                    "suggested_kind": "verify",
                    "reason": "wrongly treating file_summary as non-standard",
                },
                {
                    "task_id": "impact_check",
                    "current_kind": "verify",
                    "suggested_kind": "impact_review",
                    "reason": "change-risk review fits impact_review",
                },
            ],
            "framework_block": {"block": False, "task_ids": [], "reason": ""},
        }

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)

    should_act, reason, split_recs, kind_recs = await delegate._persona_consent_guard(
        persona="",
        user_message="Review the app backup architecture.",
        tasks=[
            {
                "task_id": "arch_client_loop_py",
                "kind": "file_summary",
                "prompt": "Summarize app/llm/client_tools_loop.py from real file evidence. Do not modify files.",
            },
            {
                "task_id": "impact_check",
                "kind": "verify",
                "prompt": "Review change impact and compatibility risks. Do not modify files.",
            },
        ],
    )

    assert should_act is True
    assert reason == "project analysis is correctly scoped"
    assert split_recs == []
    assert kind_recs == [
        {
            "task_id": "arch_client_loop_py",
            "current_kind": "file_summary",
            "suggested_kind": "verify",
            "reason": "wrongly treating file_summary as non-standard",
        },
        {
            "task_id": "impact_check",
            "current_kind": "verify",
            "suggested_kind": "impact_review",
            "reason": "change-risk review fits impact_review",
        },
    ]
    assert "- project_map:" in captured["system"]
    assert "- file_summary:" in captured["system"]
    assert "- impact_review:" in captured["system"]


async def test_delegate_guard_softens_false_detached_persona_veto(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.tools import delegate

    captured = {}

    async def fake_chat_json(messages, *args, **kwargs):
        captured["user"] = messages[1]["content"]
        return {
            "should_act": False,
            "reason": "No user request; helper task chain is detached.",
            "split_recommendations": [
                {
                    "task_id": "algo_analysis",
                    "should_split": True,
                    "split_into": ["rbtree", "skiplist", "btree", "bplus"],
                    "reason": "independent algorithms",
                }
            ],
            "kind_recommendations": [],
            "framework_block": {
                "block": True,
                "task_ids": ["algo_analysis"],
                "reason": "needs shared comparison framework",
            },
        }

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)

    result = await delegate._persona_consent_guard(
        persona="bot can handle technical document work",
        user_message="比较红黑树、跳表、B树、B+树，并输出 Word 论文。",
        tasks=[{
            "task_id": "algo_analysis",
            "kind": "code",
            "prompt": "Analyze Red-Black Tree, Skip List, B-Tree, and B+ Tree for the paper framework.",
            "expected_outputs": ["algo_analysis.json"],
        }],
    )

    should_act, reason, split_recs, kind_recs, framework = result
    assert should_act is True
    assert "No user request" in reason
    assert split_recs
    assert kind_recs == []
    assert framework["block"] is True
    assert "# Current task anchor" in captured["user"]
    assert "# Guard runtime facts" in captured["user"]
    assert "Word 论文" in captured["user"]


async def test_delegate_guard_ignores_text_artifact_demotion_to_general(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.tools import delegate

    async def fake_chat_json(messages, *args, **kwargs):
        return {
            "should_act": True,
            "reason": "ok",
            "split_recommendations": [],
            "kind_recommendations": [
                {
                    "task_id": "algo_docs",
                    "current_kind": "edit",
                    "suggested_kind": "general",
                    "reason": "mistaken lightweight text synthesis classification",
                }
            ],
            "framework_block": {"block": False, "task_ids": [], "reason": ""},
        }

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)

    should_act, reason, split_recs, kind_recs = await delegate._persona_consent_guard(
        persona="",
        user_message="Write project documentation.",
        tasks=[{
            "task_id": "algo_docs",
            "kind": "edit",
            "prompt": "Create README.md and docs/algorithm_report.md from existing implementation notes.",
            "expected_outputs": ["_env/README.md", "_env/docs/algorithm_report.md"],
        }],
    )

    assert should_act is True
    assert reason == "ok"
    assert split_recs == []
    assert kind_recs == []


def test_deterministic_guard_routes_prose_analysis_markdown_to_edit():
    """2026-06-04 P133: deterministic guard no longer applies the fuzzy "prose vs code"
    keyword heuristic. Routing of prose analyses is handled by the guard LLM with
    full task context. The deterministic guard retains only physical hard constraints
    (e.g. read/draw cannot produce executable code, code cannot produce Office docx)."""
    from app.llm.tools.delegate import _deterministic_kind_recommendations

    recs = _deterministic_kind_recommendations([
        {
            "task_id": "analysis_bplustree",
            "kind": "code",
            "prompt": (
                "Write a Chinese academic analysis of B+ Tree for a database index paper. "
                "Include theoretical comparison table, pseudocode prose, and complexity reasoning. "
                "No runnable benchmark or script is required."
            ),
            "expected_outputs": ["_helpers_shared/analysis_bplustree.md"],
        }
    ])

    # No deterministic recommendation: this is a fuzzy routing call, defer to LLM.
    assert recs == []


def test_deterministic_guard_allows_paper_framework_contract_as_edit():
    from app.llm.tools.delegate import _deterministic_kind_recommendations

    recs = _deterministic_kind_recommendations([{
        "task_id": "framework_contract",
        "kind": "edit",
        "prompt": (
            "Create a compact paper framework contract for a database-index algorithm paper. "
            "Define document outline, section ownership, file naming, acceptance checks, "
            "merge order, and final Word validation plan."
        ),
        "expected_outputs": ["_helpers_shared/paper_framework.md"],
    }])

    assert recs == []


def test_deterministic_guard_keeps_executable_framework_contract_as_code():
    """2026-06-04 P133: deterministic guard no longer applies the fuzzy
    "executable-framework-contract" heuristic; it returns no recommendation and
    lets the guard LLM (which sees the full prompt text and benchmarks/build
    commands) make the routing call."""
    from app.llm.tools.delegate import _deterministic_kind_recommendations

    correct = _deterministic_kind_recommendations([{
        "task_id": "framework_contract",
        "kind": "code",
        "prompt": (
            "Create a shared framework contract for downstream helpers. "
            "It must define runnable benchmark harness interfaces, generated dataset schema, "
            "compile commands, merge order, and expected_outputs."
        ),
        "expected_outputs": ["_helpers_shared/paper_framework.md"],
    }])
    wrong = _deterministic_kind_recommendations([{
        "task_id": "framework_contract",
        "kind": "edit",
        "prompt": (
            "Create a shared framework contract for downstream helpers. "
            "It must define runnable benchmark harness interfaces, generated dataset schema, "
            "compile commands, merge order, and expected_outputs."
        ),
        "expected_outputs": ["_helpers_shared/paper_framework.md"],
    }])

    # Deterministic guard defers both decisions to the LLM (markdown output is not
    # a hard physical mismatch for either kind).
    assert correct == []
    assert wrong == []


async def test_delegate_guard_normalizes_read_recommendation_for_text_deliverable(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.tools import delegate

    async def fake_chat_json(messages, *args, **kwargs):
        return {
            "should_act": True,
            "reason": "wrong but plausible guard routing",
            "split_recommendations": [],
            "kind_recommendations": [
                {
                    "task_id": "analysis_rbtree",
                    "current_kind": "code",
                    "suggested_kind": "read",
                    "reason": "pure analysis and markdown writing",
                }
            ],
            "framework_block": {"block": False, "task_ids": [], "reason": ""},
        }

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)

    should_act, reason, split_recs, kind_recs = await delegate._persona_consent_guard(
        persona="",
        user_message="Write a database index paper.",
        tasks=[{
            "task_id": "analysis_rbtree",
            "kind": "code",
            "prompt": "Write Red-Black Tree academic analysis for the paper.",
            "expected_outputs": ["_helpers_shared/analysis_rbtree.md"],
        }],
    )

    assert should_act is True
    assert split_recs == []
    # 2026-06-04 P133: post-LLM normalization no longer rewrites read→edit on the
    # "user-facing text artifact" heuristic. The guard LLM's recommendation is
    # passed through unchanged so the main thread can judge with full context.
    assert kind_recs == [{
        "task_id": "analysis_rbtree",
        "current_kind": "code",
        "suggested_kind": "read",
        "reason": "pure analysis and markdown writing",
    }]


async def test_delegate_guard_ignores_source_read_split_for_framework_contract(monkeypatch):
    from app.llm import client as llm_client
    from app.llm.tools import delegate

    async def fake_chat_json(messages, *args, **kwargs):
        return {
            "should_act": True,
            "reason": "mistakenly treated paper sections as source materials",
            "split_recommendations": [
                {
                    "task_id": "framework_contract",
                    "should_split": True,
                    "split_into": ["read_sources_batch_1", "read_sources_batch_2"],
                    "reason": "7 source material items/groups should be read by parallel read helpers.",
                }
            ],
            "kind_recommendations": [],
            "framework_block": {"block": False, "task_ids": [], "reason": ""},
        }

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)

    should_act, reason, split_recs, kind_recs = await delegate._persona_consent_guard(
        persona="",
        user_message="Write a Word paper comparing database index structures.",
        tasks=[{
            "task_id": "framework_contract",
            "kind": "edit",
            "prompt": (
                "Create a paper framework contract. Define the document outline, section ownership, "
                "file naming, acceptance checks, merge order, and final Word validation plan."
            ),
            "expected_outputs": ["framework_contract.md"],
        }],
    )

    assert should_act is True
    assert "source materials" in reason
    assert split_recs == []
    assert kind_recs == []


def test_readonly_internal_report_request_is_not_retry_blocking():
    result = {
        "ok": True,
        "results": [{
            "task_id": "code_index_top3",
            "kind": "project_map",
            "ok": False,
            "terminal_reason": "resource_required",
            "summary": "Structural analysis complete. " * 20,
            "resource_required": {
                "suggested_helper_kind": "edit",
                "blocked_reason": (
                    "This is a read-only helper and cannot write the report file. "
                    "Need an edit helper to write _helper_report_top3.md."
                ),
                "needed_outputs": ["_helper_report_top3.md"],
            },
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": ["_helper_report_top3.md"],
            },
        }],
    }

    assert _retryable_delegate_facts_from_result(result) == []


def test_code_missing_output_still_requires_retry():
    result = {
        "ok": True,
        "results": [{
            "task_id": "impl",
            "kind": "code",
            "ok": False,
            "terminal_reason": "outputs_missing",
            "summary": "Started but did not create the file.",
            "outputs_check": {
                "outputs_complete": False,
                "outputs_missing": ["src/graph.py"],
            },
        }],
    }

    facts = _retryable_delegate_facts_from_result(result)

    assert len(facts) == 1
    assert facts[0]["task_id"] == "impl"
    assert facts[0]["terminal_reason"] == "outputs_missing"
