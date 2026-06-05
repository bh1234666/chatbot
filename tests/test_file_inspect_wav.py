import struct
import wave

from app.llm.tools.workspace import inspect_file


def test_inspect_file_reports_pcm_wav(tmp_path):
    wav_path = tmp_path / "pcm.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x00\x00" * 2400)

    result = inspect_file(str(tmp_path), "pcm.wav")

    assert result["ok"] is True
    assert result["metadata"]["audio_format"] == 1
    assert result["metadata"]["audio_format_name"] == "PCM"
    assert result["metadata"]["bits_per_sample"] == 16
    assert "warnings" not in result


def test_inspect_file_warns_for_ieee_float_wav_with_fact_chunk(tmp_path):
    wav_path = tmp_path / "float.wav"
    sample_rate = 24000
    channels = 1
    bits_per_sample = 32
    frames = 2400
    data_size = frames * channels * bits_per_sample // 8
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    chunks = [
        b"fmt " + struct.pack("<IHHIIHH", 16, 3, channels, sample_rate, byte_rate, block_align, bits_per_sample),
        b"fact" + struct.pack("<II", 4, frames),
        b"data" + struct.pack("<I", data_size) + (b"\x00" * data_size),
    ]
    body = b"".join(chunks)
    wav_path.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body)

    result = inspect_file(str(tmp_path), "float.wav")

    assert result["ok"] is True
    assert result["metadata"]["audio_format"] == 3
    assert result["metadata"]["audio_format_name"] == "IEEE_FLOAT"
    assert result["metadata"]["bits_per_sample"] == 32
    assert "WAV is not PCM encoded; some tools may fail to read it" in result["warnings"]
