import csv
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SEG_S = 4
KEEP_S = 600
ANCHOR_WINDOW = 8
GAP_S = 3.0
MAX_ROWS = 1000                                    # last rows read per run; the ring keeps far fewer


def pipeline_argv(url: str, run_dir: Path, ffmpeg: str) -> tuple[list[str], list[str]]:
    streamlink = [str(Path(sys.executable).with_name("streamlink")), "--stdout", "--retry-streams", "30",
                  "--retry-open", "3", "--hls-live-edge", "3", url, "720p60,720p,best"]
    ffmpeg_argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c", "copy", "-f", "segment",
                   "-segment_time", str(SEG_S), "-segment_format", "mpegts", "-reset_timestamps", "1",
                   "-segment_list", f"{run_dir}/list.csv", "-segment_list_type", "csv", "-segment_list_flags",
                   "+live", f"{run_dir}/%08d.ts"]
    return streamlink, ffmpeg_argv


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    end: float
    gap_before: bool


def _runs(buf: Path) -> list[Path]:
    """Run directories only: real (not symlinked) direct children with an all-digit name."""
    return sorted((p for p in buf.iterdir() if p.name.isdigit() and not p.is_symlink() and p.is_dir()),
                  key=lambda p: int(p.name))


def _rows(run: Path) -> list[tuple[Path, float, float, float]]:
    """(path, stream start, stream end, mtime) for complete, sane rows whose file still exists."""
    try:
        text = (run / "list.csv").read_text()
    except OSError:
        return []
    lines = text.split("\n")[:-1]                  # a last line without its newline is still being written
    rows, prev_end = [], None
    for row in csv.reader(lines[-MAX_ROWS:]):
        try:
            name, s, e = row[0], float(row[1]), float(row[2])
        except (IndexError, ValueError):
            continue
        if not (math.isfinite(s) and math.isfinite(e) and e > s) or (prev_end is not None and s < prev_end - 0.5):
            continue                                # bad numbers, duplicates and rows that go backwards
        prev_end = e
        try:
            rows.append((run / name, s, e, (run / name).stat().st_mtime))
        except OSError:                             # pruned
            continue
    return rows


# ponytail: forward-min anchor assumes bursts shorter than ANCHOR_WINDOW segments;
# a skipped stretch shorter than GAP_S is not flagged. Parse streamlink's ad log lines if that bites.
def index(buf: Path, *, settled: bool = False) -> list[Segment]:
    segments, runs = [], _runs(buf)
    for run in runs:
        rows = _rows(run)
        offsets = [mtime - end for _, _, end, mtime in rows]
        anchors = [min(offsets[i:i + ANCHOR_WINDOW]) for i in range(len(rows))]
        previous_end = previous_anchor = None
        still_growing = settled and run == runs[-1]   # an earlier run is finished: its anchors are final
        usable = max(0, len(rows) - ANCHOR_WINDOW + 1) if still_growing else len(rows)
        for (path, stream_start, stream_end, _), anchor in zip(rows[:usable], anchors):
            gap = (previous_end is None or stream_start - previous_end > 1.0 or
                   anchor - previous_anchor > GAP_S)
            segments.append(Segment(path, stream_start + anchor, stream_end + anchor, gap))
            previous_end, previous_anchor = stream_end, anchor
    return segments


def has_settled_tail(buf: Path, end: float) -> bool:
    """Whether the segment covering end already has its full window of future anchors."""
    try:
        return any(segment.start < end <= segment.end for segment in index(buf, settled=True))
    except FileNotFoundError:
        return False


def covered_window(buf: Path, start: float, end: float, *, core_start: float,
                   core_end: float) -> tuple[float, float] | None:
    """Keep as much context as possible from a continuous span that covers the required event."""
    segments = index(buf)
    first = 0
    for boundary in range(1, len(segments) + 1):
        if boundary < len(segments) and not segments[boundary].gap_before:
            continue
        span_start, span_end = segments[first].start, segments[boundary - 1].end
        if span_start <= core_start and span_end >= core_end:
            return max(start, span_start), min(end, span_end)
        first = boundary
    return None


@dataclass(frozen=True)
class RawClip:
    path: Path
    t0: float
    duration: float


def cut(buf: Path, start: float, end: float, out: Path, ffmpeg: str) -> RawClip | None:
    selected = [s for s in index(buf) if s.end > start and s.start < end]
    if (not selected or selected[0].start > start or selected[-1].end < end or
            any(s.gap_before for s in selected[1:])):
        return None

    list_path = out.with_suffix(".txt")
    with list_path.open("w") as f:
        for segment in selected:
            path = str(segment.path.resolve()).replace("'", "'\\''")
            f.write(f"file '{path}'\n")
    t0, want = selected[0].start, selected[-1].end - selected[0].start
    try:
        # concat can exit 0 after failing to open an input, so the output's real length is checked.
        made = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
                               str(list_path), "-c", "copy", str(out)], timeout=120).returncode == 0
        probe = subprocess.run([str(Path(ffmpeg).with_name("ffprobe")), "-v", "error", "-show_entries",
                                "format=duration", "-of", "csv=p=0", str(out)], capture_output=True, text=True, timeout=120)
        got = float(probe.stdout.strip()) if made and probe.returncode == 0 else -1.0
    except (OSError, ValueError, subprocess.SubprocessError):     # a timeout is a failed cut
        got = -1.0
    finally:
        list_path.unlink(missing_ok=True)
    if got < want - 1.0:
        out.unlink(missing_ok=True)
        return None
    return RawClip(out, t0, want)


def prune(buf: Path, now: float, keep_s: int = KEEP_S) -> int:
    runs = _runs(buf)
    newest = runs[-1] if runs else None
    deleted = 0
    for run in runs:
        for path in run.glob("*.ts"):
            if path.lstat().st_mtime < now - keep_s:
                path.unlink()                       # unlink never follows a symlink
                deleted += 1
        if run != newest and not any(run.glob("*.ts")):
            (run / "list.csv").unlink(missing_ok=True)
            try:
                run.rmdir()
            except OSError:                         # something else is in there; leave it
                pass
    return deleted
