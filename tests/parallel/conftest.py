"""Shared fixtures for parallel tests."""
import pytest
import tempfile
import os


@pytest.fixture
def tmp_workspace():
    """Create a temporary workspace directory that cleans up after test."""
    with tempfile.TemporaryDirectory(prefix="test_ws_") as d:
        yield d


@pytest.fixture
def fake_workspace(tmp_path):
    """Construct workspace structure resembling real trace layouts."""
    (tmp_path / "merge_charts_chart1.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "merge_charts_chart2.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "report.docx").write_bytes(b"PK")

    shared = tmp_path / "_helpers_shared" / "merge_charts"
    shared.mkdir(parents=True)
    (shared / "chart1.png").write_bytes(b"\x89PNG\r\n")

    delegate_dir = tmp_path / "_delegate_user1_paper_pptx"
    delegate_dir.mkdir()
    (delegate_dir / "chart1.png").write_bytes(b"\x89PNG\r\n")

    skip = tmp_path / "_downloaded_media"
    skip.mkdir()
    (skip / "chart1.png").write_bytes(b"DOWNLOADED")

    return tmp_path
