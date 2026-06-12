def test_promote_to_main_does_not_promote_delegate_internal_file(tmp_path):
    from app.llm.tools.workspace import promote_to_main

    main_ws = tmp_path / "main"
    temp_ws = main_ws / ".temp"
    internal = temp_ws / "_delegate_old_task" / "old_assignment.md"
    internal.parent.mkdir(parents=True)
    internal.write_text("old internal output\n", encoding="utf-8")

    promoted, skipped, remap = promote_to_main(
        str(main_ws),
        str(temp_ws),
        ["old_assignment.md"],
    )

    assert promoted == []
    assert skipped == ["old_assignment.md"]
    assert remap == {}
    assert not (main_ws / "old_assignment.md").exists()


def test_promote_to_main_skips_ambiguous_prefixed_matches(tmp_path):
    from app.llm.tools.workspace import promote_to_main

    main_ws = tmp_path / "main"
    temp_ws = main_ws / ".temp"
    temp_ws.mkdir(parents=True)
    (temp_ws / "draft_a_report.md").write_text("a\n", encoding="utf-8")
    (temp_ws / "draft_b_report.md").write_text("b\n", encoding="utf-8")

    promoted, skipped, remap = promote_to_main(
        str(main_ws),
        str(temp_ws),
        ["report.md"],
    )

    assert promoted == []
    assert skipped == ["report.md"]
    assert remap == {}
    assert not (main_ws / "draft_a_report.md").exists()
    assert not (main_ws / "draft_b_report.md").exists()


def test_promote_to_main_allows_unique_helper_prefixed_match(tmp_path):
    from app.llm.tools.workspace import promote_to_main

    main_ws = tmp_path / "main"
    temp_ws = main_ws / ".temp"
    temp_ws.mkdir(parents=True)
    source = temp_ws / "writer_report.md"
    source.write_text("final\n", encoding="utf-8")

    promoted, skipped, remap = promote_to_main(
        str(main_ws),
        str(temp_ws),
        ["report.md"],
    )

    assert promoted == ["writer_report.md"]
    assert skipped == []
    assert remap == {"report.md": "writer_report.md"}
    assert (main_ws / "writer_report.md").read_text(encoding="utf-8") == "final\n"
