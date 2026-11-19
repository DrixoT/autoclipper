import csv
import os
import subprocess
from pathlib import Path

import pytest

from autoclipper.capture import cut, index, prune

FFMPEG = "/opt/homebrew/bin/ffmpeg"


def run_dir(buf, name, rows):
    """rows: [(stream_start, stream_end, wall time the file was closed)]"""
    d = buf / name; d.mkdir(parents=True)
    with open(d / "list.csv", "w") as f:
        for i, (s, e, w) in enumerate(rows):
            p = d / f"{i:08d}.ts"; p.write_bytes(b"x"); os.utime(p, (w, w))
            f.write(f"{p.name},{s:.6f},{e:.6f}\n")
    return d


def smooth(n, base=1000.0, first=0):
    return [(4.0 * i, 4.0 * i + 4, base + 4.0 * i + 4) for i in range(first, first + n)]


def test_index_smooth_arrival(tmp_path):
    run_dir(tmp_path, "1000", smooth(10))
    segs = index(tmp_path)
    assert [(s.start, s.end) for s in segs] == [(1000.0 + 4 * i, 1004.0 + 4 * i) for i in range(10)]
    assert [s.gap_before for s in segs] == [True] + [False] * 9


def test_index_removes_burst_jitter(tmp_path):
    rows = [(4.0 * i, 4.0 * i + 4, 1000.0 + 12 * (i // 3 + 1)) for i in range(12)]   # three files land at once
    run_dir(tmp_path, "1000", rows)
    segs = index(tmp_path)
    assert [s.start for s in segs] == [1000.0 + 4 * i for i in range(12)]
    assert not any(s.gap_before for s in segs[1:])


def test_index_flags_a_skipped_stretch(tmp_path):                                     # an ad: stream time continues, wall jumps 30 s
    run_dir(tmp_path, "1000", smooth(6) + smooth(6, base=1030.0, first=6))
    segs = index(tmp_path)
    assert [s.gap_before for s in segs] == [True] + [False] * 5 + [True] + [False] * 5
    assert segs[6].start == 1054.0
    assert cut(tmp_path, 1010.0, 1060.0, tmp_path / "o.ts", "ffmpeg") is None


def test_index_flags_a_stream_time_jump(tmp_path):
    run_dir(tmp_path, "1000", smooth(3) + [(42.0, 46.0, 1046.0), (46.0, 50.0, 1050.0)])
    assert [s.gap_before for s in index(tmp_path)] == [True, False, False, True, False]


def test_two_runs_never_join(tmp_path):
    run_dir(tmp_path, "1000", smooth(4)); run_dir(tmp_path, "1016", smooth(4, base=1016.0))
    segs = index(tmp_path)
    assert len(segs) == 8 and segs[4].gap_before and segs[4].start == 1016.0
    assert cut(tmp_path, 1010.0, 1022.0, tmp_path / "o.ts", "ffmpeg") is None


def test_index_skips_pruned_files(tmp_path):
    d = run_dir(tmp_path, "1000", smooth(5)); (d / "00000000.ts").unlink()
    assert [s.path.name for s in index(tmp_path)] == [f"{i:08d}.ts" for i in range(1, 5)]


def test_cut_returns_none_when_window_not_covered(tmp_path):
    run_dir(tmp_path, "1000", smooth(3))
    assert cut(tmp_path, 990.0, 1003.0, tmp_path / "o.ts", "ffmpeg") is None
    assert cut(tmp_path, 1002.0, 1030.0, tmp_path / "o.ts", "ffmpeg") is None


def test_prune_removes_old_segments_and_empty_old_runs(tmp_path):
    old = run_dir(tmp_path, "100", smooth(2, base=100.0)); new = run_dir(tmp_path, "990", smooth(2, base=990.0))
    assert prune(tmp_path, now=1000.0, keep_s=600) == 2
    assert not old.exists() and sorted(p.name for p in new.glob("*.ts")) == ["00000000.ts", "00000001.ts"]


def real_run(buf, name):
    """Four real 4 s segments in buf/<name>, mtimes set as if they arrived smoothly from wall time <name>."""
    d = buf / name; d.mkdir()
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=16:r=30", "-f", "lavfi", "-i", "sine=d=16",
                    "-c:v", "h264_videotoolbox", "-g", "60", "-c:a", "aac", "-f", "segment", "-segment_time", "4",
                    "-segment_format", "mpegts", "-reset_timestamps", "1", "-segment_list", str(d / "list.csv"),
                    "-segment_list_type", "csv", str(d / "%08d.ts")], check=True)
    for row in csv.reader(open(d / "list.csv")):
        os.utime(d / row[0], (int(name) + float(row[2]), int(name) + float(row[2])))
    return d

@pytest.mark.skipif(not Path(FFMPEG).exists(), reason="ffmpeg not installed")
def test_cut_concatenates_real_segments(tmp_path):
    real_run(tmp_path, "5000")
    raw = cut(tmp_path, 5001.0, 5009.0, tmp_path / "o.ts", FFMPEG)
    assert abs(raw.t0 - 5000.0) < 0.01 and 11.0 <= raw.duration <= 13.0 and raw.path.stat().st_size > 0

@pytest.mark.skipif(not Path(FFMPEG).exists(), reason="ffmpeg not installed")
def test_cut_returns_none_when_an_input_vanishes(tmp_path, monkeypatch):
    d = real_run(tmp_path, "5000")
    import autoclipper.capture as cap
    real = cap.subprocess.run
    def run(argv, **kw):
        (d / "00000001.ts").unlink(missing_ok=True)      # disappears after selection, before ffmpeg opens it
        return real(argv, **kw)
    monkeypatch.setattr(cap.subprocess, "run", run)
    out = tmp_path / "o.ts"
    assert cut(tmp_path, 5001.0, 5009.0, out, FFMPEG) is None and not out.exists()
    assert not list(tmp_path.glob("*.txt"))

def test_prune_and_index_ignore_symlinks_and_other_directories(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    old = outside / "00000000.ts"; old.write_bytes(b"x"); os.utime(old, (1, 1))
    buf = tmp_path / "buf"; buf.mkdir()
    (buf / "1000").symlink_to(outside, target_is_directory=True)
    other = buf / "kick-stream" / "1000"; other.mkdir(parents=True)
    keep = other / "00000000.ts"; keep.write_bytes(b"x"); os.utime(keep, (1, 1))
    run_dir(buf, "2000", smooth(2, base=2000.0))
    assert [s.path.parent.name for s in index(buf)] == ["2000", "2000"]
    assert prune(buf, now=5000.0, keep_s=600) == 2
    assert old.exists() and keep.exists() and (buf / "2000").is_dir()

def test_index_flags_a_six_second_skip(tmp_path):
    run_dir(tmp_path, "1000", smooth(6) + smooth(6, base=1006.0, first=6))
    assert [s.gap_before for s in index(tmp_path)] == [True] + [False] * 5 + [True] + [False] * 5
    assert cut(tmp_path, 1010.0, 1040.0, tmp_path / "o.ts", "ffmpeg") is None

def test_index_ignores_incomplete_and_invalid_rows(tmp_path):
    d = run_dir(tmp_path, "1000", smooth(4))
    (d / "00000004.ts").write_bytes(b"x"); (d / "00000005.ts").write_bytes(b"x")
    with open(d / "list.csv", "a") as f:
        f.write("00000004.ts,nan,20.0\n00000005.ts,20.000000,2")     # a bad number, then a row still being written
    assert [s.path.name for s in index(tmp_path)] == [f"{i:08d}.ts" for i in range(4)]

def test_index_skips_duplicate_and_reversed_rows(tmp_path):
    d = run_dir(tmp_path, "1000", smooth(3))
    with open(d / "list.csv", "a") as f:
        f.write("00000001.ts,4.000000,8.000000\n00000002.ts,12.000000,8.000000\n")
    assert len(index(tmp_path)) == 3


def test_cut_treats_an_ffmpeg_timeout_as_a_failed_cut(tmp_path, monkeypatch):
    import autoclipper.capture as cap
    run_dir(tmp_path, "1000", smooth(4))
    seen = []
    def hang(argv, **kw):
        seen.append(kw.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
    monkeypatch.setattr(cap.subprocess, "run", hang)
    out = tmp_path / "o.ts"
    assert cut(tmp_path, 1001.0, 1009.0, out, "ffmpeg") is None and not out.exists() and seen[0] == 120


def test_cut_readiness_waits_for_the_target_segments_future_anchors(tmp_path):
    from autoclipper.capture import has_settled_tail

    run_dir(tmp_path, "1000", smooth(10))
    assert has_settled_tail(tmp_path, 1012.0)
    assert not has_settled_tail(tmp_path, 1012.1)


def test_coverage_can_cut_a_gapped_tail_but_cannot_skip_the_event(tmp_path):
    from autoclipper.capture import covered_window

    run_dir(tmp_path, "1000", smooth(6) + smooth(6, base=1030.0, first=6))
    assert covered_window(tmp_path, 1001, 1060, core_start=1008, core_end=1020) == (1001, 1024)
    assert covered_window(tmp_path, 1001, 1060, core_start=1010, core_end=1057) is None


def test_a_finished_runs_tail_is_settled_once_a_newer_run_exists(tmp_path):
    from autoclipper.capture import has_settled_tail

    run_dir(tmp_path, "1000", smooth(10))                    # 1000..1040, then the capture restarted
    assert not has_settled_tail(tmp_path, 1038.0)            # the newest run: its tail still waits for anchors
    run_dir(tmp_path, "2000", smooth(3, base=2000.0))
    assert has_settled_tail(tmp_path, 1038.0)                # finished: no more anchors will come
    assert not has_settled_tail(tmp_path, 2010.0)            # the new run's own tail still waits

