from pathlib import Path


def test_longrun_materials_are_isolated_per_run():
    src = Path("_longrun_test/scenario_runner.py").read_text(encoding="utf-8")

    assert "run_root = OUT / \"runs\" / run_id" in src
    assert "PROJECTS / \"mini_app\"" not in src
    assert "shutil.rmtree(mini)" not in src
    assert "shutil.rmtree(green)" not in src
