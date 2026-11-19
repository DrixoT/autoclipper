import math
import re
import subprocess
from pathlib import Path

FREEZE_NOISE, FREEZE_MIN_S = "-60dB", 2.0
SILENCE_NOISE, SILENCE_MIN_S = "-50dB", 2.0
TIMEOUT_S = 120

_FREEZE_START = re.compile(r"freeze_start:\s*([-+\d.eE]+)")
_FREEZE_END = re.compile(r"freeze_end:\s*([-+\d.eE]+)")
_SILENCE_START = re.compile(r"silence_start:\s*([-+\d.eE]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([-+\d.eE]+)")


def _fraction(spans: list[tuple[float, float]], duration: float) -> float:
    if not math.isfinite(duration) or duration <= 0:
        return 0.0
    clipped = sorted((max(0.0, start), min(duration, end)) for start, end in spans
                     if math.isfinite(start) and math.isfinite(end) and end > 0 and start < duration)
    total = 0.0
    if clipped:
        start, end = clipped[0]
        for next_start, next_end in clipped[1:]:
            if next_start <= end:
                end = max(end, next_end)
            else:
                total += end - start
                start, end = next_start, next_end
        total += end - start
    return min(1.0, max(0.0, total / duration))


def _spans(stderr: str, start_pattern: re.Pattern, end_pattern: re.Pattern, duration: float):
    spans = []
    opened = None
    for line in stderr.splitlines():
        start_match = start_pattern.search(line)
        end_match = end_pattern.search(line)
        if start_match:
            try:
                value = float(start_match.group(1))
                if math.isfinite(value):
                    if opened is not None:
                        spans.append((opened, value))
                    opened = value
            except ValueError:
                pass
        if end_match:
            try:
                value = float(end_match.group(1))
                if math.isfinite(value) and opened is not None:
                    spans.append((opened, value))
                    opened = None
            except ValueError:
                pass
    if opened is not None:
        spans.append((opened, duration))
    return spans


def parse(stderr: str, duration: float) -> tuple[float, float]:
    freezes = _spans(stderr, _FREEZE_START, _FREEZE_END, duration)
    silences = _spans(stderr, _SILENCE_START, _SILENCE_END, duration)
    return _fraction(freezes, duration), _fraction(silences, duration)


def detect(raw: Path, ffmpeg: str, duration: float) -> tuple[float, float]:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", raw, "-vf",
         f"freezedetect=n={FREEZE_NOISE}:d={FREEZE_MIN_S}", "-af",
         f"silencedetect=n={SILENCE_NOISE}:d={SILENCE_MIN_S}", "-f", "null", "-"],
        capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
    )
    return parse(result.stderr, duration)
