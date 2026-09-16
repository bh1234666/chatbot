from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_critical_batch_launchers_use_repo_python_entrypoint():
    targets = [
        "start.bat",
        "startbot.bat",
        "startbot_nogpu.bat",
        "startbot_full.bat",
        "startbot_full_nogpu.bat",
        "start_backend.bat",
        "start_no_gpu.bat",
        "start_qqbot.bat",
        "start_agent.bat",
        "start_no_gpu.bat",
        "botctl.bat",
        "auto_publish.bat",
        "export_public_snapshot.bat",
    ]

    for rel in targets:
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        assert "repo_python.bat" in text, rel


def test_critical_batch_launchers_do_not_directly_run_venv_python():
    targets = [
        "start.bat",
        "startbot.bat",
        "startbot_nogpu.bat",
        "startbot_full.bat",
        "startbot_full_nogpu.bat",
        "start_qqbot.bat",
        "start_agent.bat",
        "botctl.bat",
        "auto_publish.bat",
        "export_public_snapshot.bat",
    ]

    for rel in targets:
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith(".venv\\Scripts\\python.exe"), rel
            assert not stripped.startswith('".venv\\Scripts\\python.exe"'), rel
            assert "&& .venv\\Scripts\\python.exe " not in stripped, rel


def test_no_gpu_launcher_disables_gpu_features_for_child_processes():
    agent = (ROOT / "start_no_gpu.bat").read_text(encoding="utf-8", errors="ignore")
    for setting in (
        'set "GPU_DISABLED=true"',
        'set "VISION_ENABLED=false"',
        'set "VOICE_ENABLED=false"',
        'set "STARTUP_OCR_WARM_ENABLED=false"',
        'set "CUDA_VISIBLE_DEVICES=-1"',
        'set "NVIDIA_VISIBLE_DEVICES=none"',
    ):
        assert setting in agent
    assert "call start_backend.bat" in agent
    assert "call start_agent.bat" in agent

    qq = (ROOT / "startbot_nogpu.bat").read_text(encoding="utf-8", errors="ignore")
    assert "call startbot.bat --no-gpu --no-model-vision --no-image-gen" in qq


def test_full_qq_launchers_compose_independent_feature_switches():
    full = (ROOT / "startbot_full.bat").read_text(encoding="utf-8", errors="ignore")
    full_nogpu = (ROOT / "startbot_full_nogpu.bat").read_text(encoding="utf-8", errors="ignore")

    assert "call startbot.bat --gpu --model-vision --image-gen --voice" in full
    assert "call startbot.bat --no-gpu --model-vision --image-gen" in full_nogpu

    base = (ROOT / "startbot.bat").read_text(encoding="utf-8", errors="ignore")
    for flag in (
        "--model-vision", "--local-ocr", "--no-model-vision",
        "--image-gen", "--no-image-gen", "--no-gpu", "--gpu",
        "--voice", "--no-voice",
        "--show-features",
    ):
        assert flag in base
