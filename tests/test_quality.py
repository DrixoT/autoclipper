import subprocess
from pathlib import Path

import pytest

from autoclipper.quality import detect, parse

FFMPEG = "/opt/homebrew/bin/ffmpeg"


def test_parse_counts_closed_and_open_spans_without_double_counting():
    stderr = """
[freezedetect] lavfi.freezedetect.freeze_start: 1
[freezedetect] lavfi.freezedetect.freeze_duration: 3
[freezedetect] lavfi.freezedetect.freeze_end: 4
[freezedetect] lavfi.freezedetect.freeze_start: 6
[freezedetect] lavfi.freezedetect.freeze_duration: 2
[freezedetect] lavfi.freezedetect.freeze_end: 8
[silencedetect] silence_start: 2
[silencedetect] silence_end: 5 | silence_duration: 3
[silencedetect] silence_start: 8
"""
    assert parse(stderr, 10) == (0.5, 0.5)


def test_parse_nothing_is_zero():
    assert parse("ffmpeg completed successfully", 10) == (0.0, 0.0)


def test_parse_clamps_and_merges_repeated_spans():
    stderr = """
lavfi.freezedetect.freeze_start: -2
lavfi.freezedetect.freeze_end: 4
lavfi.freezedetect.freeze_start: 2
lavfi.freezedetect.freeze_end: 12
silence_start: 3
silence_end: 8 | silence_duration: 5
silence_start: 5
silence_end: 9 | silence_duration: 4
"""
    assert parse(stderr, 10) == (1.0, 0.6)


@pytest.mark.skipif(not Path(FFMPEG).exists(), reason="ffmpeg not installed")
def test_detect_measures_still_and_silence(tmp_path):
    raw = tmp_path / "raw.mp4"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=6",
        "-f", "lavfi", "-i", "testsrc=s=320x240:r=25:d=6",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=6",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a][3:a]concat=n=2:v=0:a=1[a]",
        "-map", "[v]", "-map", "[a]", "-t", "12", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(raw),
    ], check=True)

    frozen, silent = detect(raw, FFMPEG, 12)
    assert abs(frozen - 0.5) <= 0.15
    assert abs(silent - 0.5) <= 0.15


@pytest.mark.skipif(not Path(FFMPEG).exists(), reason="ffmpeg not installed")
def test_detect_without_audio_returns_zero_silence(tmp_path):
    raw = tmp_path / "video-only.mp4"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "color=c=black:s=160x120:r=25:d=3", "-c:v", "mpeg4", str(raw),
    ], check=True)
    _, silent = detect(raw, FFMPEG, 3)
    assert silent == 0.0
