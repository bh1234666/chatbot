def test_code_repair_expected_outputs_gain_staged_source_ownership():
    from app.llm.tools.delegate import _augment_code_repair_expected_outputs

    result = _augment_code_repair_expected_outputs(
        kind="code",
        prompt=(
            "Fix config_loader.py so precedence is correct. "
            "Read app_config.py and tests/test_config_loader.py for context. "
            "Write a short fix_report.md."
        ),
        input_files=["config_loader.py", "app_config.py", "tests/test_config_loader.py"],
        expected_outputs=["fix_report.md"],
    )

    assert result == ["fix_report.md", "_env/config_loader.py"]


def test_code_repair_expected_outputs_preserve_existing_source_ownership():
    from app.llm.tools.delegate import _augment_code_repair_expected_outputs

    result = _augment_code_repair_expected_outputs(
        kind="code",
        prompt="Fix src/app.py and report the result.",
        input_files=["src/app.py", "tests/test_app.py"],
        expected_outputs=["_env/src/app.py", "fix_report.md"],
    )

    assert result == ["_env/src/app.py", "fix_report.md"]


def test_code_repair_expected_outputs_do_not_add_context_only_sources():
    from app.llm.tools.delegate import _augment_code_repair_expected_outputs

    result = _augment_code_repair_expected_outputs(
        kind="code",
        prompt="Fix config_loader.py using app_config.py as context.",
        input_files=["config_loader.py", "app_config.py"],
        expected_outputs=["fix_report.md"],
    )

    assert result == ["fix_report.md", "_env/config_loader.py"]


def test_code_repair_expected_outputs_gain_multiple_explicit_edit_sources():
    from app.llm.tools.delegate import _augment_code_repair_expected_outputs

    prompt = (
        "Migration task: rename customer_name to account_name in two mini-repos. "
        "The tests are already forward-looking, so only fix the implementations.\n\n"
        "## Files to edit\n\n"
        "### contracts/customer_event.py\n"
        "Currently uses customer_name in validation and return dict. "
        "Update the implementation to use account_name.\n\n"
        "### service/render.py\n"
        "Currently reads event['customer_name']. "
        "Update the implementation to use account_name.\n\n"
        "## Requirements\n"
        "- Only change the implementation files (contracts/customer_event.py, service/render.py).\n"
        "- Do NOT change the test files; they are already correct."
    )

    result = _augment_code_repair_expected_outputs(
        kind="code",
        prompt=prompt,
        input_files=["contracts/customer_event.py", "service/render.py"],
        expected_outputs=[],
    )

    assert result == ["_env/contracts/customer_event.py", "_env/service/render.py"]


def test_code_repair_expected_outputs_ignore_read_context_after_edit_signal():
    from app.llm.tools.delegate import _augment_code_repair_expected_outputs

    result = _augment_code_repair_expected_outputs(
        kind="code",
        prompt=(
            "Fix config_loader.py so precedence is correct. "
            "Use app_config.py as context and read docs/config.md for background."
        ),
        input_files=["config_loader.py", "app_config.py", "docs/config.md"],
        expected_outputs=["fix_report.md"],
    )

    assert result == ["fix_report.md", "_env/config_loader.py"]
