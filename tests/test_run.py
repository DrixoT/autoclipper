import asyncio
import contextlib
from collections import Counter
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import autoclipper.run as run
from autoclipper.capture import RawClip
from autoclipper.config import Settings, StreamCfg
from autoclipper.detect import Event
from autoclipper.meta import make_edit
from autoclipper.render import Word
from autoclipper.store import add_clip, flag, open_db, pause, paused, set_state
from autoclipper.upload import QuotaExhausted, UploadLimit
from autoclipper.run import guard_once, process_clip, slot_once, stream_task

ORIGINAL_CHOOSE = run.choose_clips          # the autouse fixture below swaps it out for most tests
ORIGINAL_GAME = run.fixed_game              # likewise: no test asks Twitch, Kick or streamlink for a category


@pytest.fixture(autouse=True)
def disk_is_fine(monkeypatch):
    """slot_once and _cut_later check the disk (audit A7); tests that need a full disk override this."""
    monkeypatch.setattr("autoclipper.run.disk_ok", lambda s: True)
    monkeypatch.setattr(run, "choose_clips", lambda db, s, rows: list(rows))   # ranking has its own tests (round 3)
    monkeypatch.setattr(run, "SECRETS", {"jev": None, "twitch": None, "ok": True})   # main reads the Keychain once
    def no_keychain(*a): raise AssertionError("the Keychain is read only once, at startup")
    monkeypatch.setattr("keyring.get_password", no_keychain)
    monkeypatch.setattr(run, "fixed_game", lambda cfg: None)
    run.BUFS.clear()
    run.STOP.clear()
    run.CHECKED.clear()


class Proc:                                   # fake capture pair that has already exited
    def __init__(self, alive=False): self.alive = alive
    def poll(self): return None if self.alive else 1
    def terminate(self): self.alive = False

async def no_chat(cfg):
    if False: yield

def test_stream_task_restarts_after_capture_exit(tmp_path, monkeypatch):
    monkeypatch.setattr("autoclipper.run.disk_ok", lambda s: True)
    spawned, slept = [], []
    def spawn(cfg, s):
        spawned.append(cfg.key); return Proc(alive=False)
    async def sleep(n):
        slept.append(n)
        if len(slept) == 3: raise asyncio.CancelledError
    s = Settings(streams=(StreamCfg("twitch", "a"),), data_dir=tmp_path, volume=tmp_path)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stream_task(s.streams[0], s, open_db(tmp_path / "a.db"), spawn=spawn, chat=no_chat, sleep=sleep))
    assert spawned == ["twitch:a"] * 3 and slept == [5, 10, 20]

def test_capture_pauses_when_disk_not_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("autoclipper.run.disk_ok", lambda s: False)
    spawned = []
    async def sleep(n): raise asyncio.CancelledError
    s = Settings(streams=(StreamCfg("twitch", "a"),), data_dir=tmp_path, volume=tmp_path)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stream_task(s.streams[0], s, open_db(tmp_path / "a.db"),
                                spawn=lambda *a: spawned.append(1), chat=no_chat, sleep=sleep))
    assert spawned == []

def _db(tmp_path, scores):
    db = open_db(tmp_path / "a.db")
    ids = [add_clip(db, "twitch:a", 900.0, 920.0, sc, "funny", ["LUL"], "/x/r.ts", 870.0) for sc in scores]
    return db, ids

def test_slot_processes_the_best_clip(tmp_path):
    db, ids = _db(tmp_path, [5.0, 9.0]); done = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    got = asyncio.run(slot_once(s, db, process=lambda db, s, row: done.append(row["id"]), now=lambda: 1000.0))
    assert got == ids[1] and done == [ids[1]]

def test_slot_skips_when_paused_or_at_daily_cap(tmp_path):
    db, ids = _db(tmp_path, [5.0]); done = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, max_uploads_per_day=1)
    pause(db, "primary", "strike")
    assert asyncio.run(slot_once(s, db, process=lambda *a: done.append(1), now=lambda: 1000.0)) is None
    pause(db, "primary", "")
    set_state(db, ids[0], "uploaded", video_id="v", uploaded_at=990.0)
    add_clip(db, "twitch:a", 950.0, 960.0, 7.0, "hype", ["W"], "/x/r2.ts", 920.0)
    assert asyncio.run(slot_once(s, db, process=lambda *a: done.append(1), now=lambda: 1000.0)) is None
    assert done == []

def test_guard_pauses_uploads_on_a_strike(tmp_path):
    db, ids = _db(tmp_path, [5.0]); set_state(db, ids[0], "uploaded", video_id="v1", uploaded_at=1.0)
    notes = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    asyncio.run(guard_once(s, db, check=lambda ids, profile: ["v1"], notify=notes.append))
    assert paused(db, "primary") == "strike:v1" and len(notes) == 1


# --- beyond the plan: the event-to-clip path, chat reconnects, and process_clip with every external call faked

def _one_event(tmp_path, monkeypatch, cfg, cut_fn=None):
    """Run stream_task through one detected event and one capture exit; returns (cuts, chat opens, db)."""
    monkeypatch.setattr(run, "TICK_S", 0.01); monkeypatch.setattr(run, "CUT_DELAY_S", 0)
    monkeypatch.setattr(run, "CHAT_RETRY_S", (0.01, 0.02))
    ev = Event("twitch:a", 1000.0, 1010.0, 5.0, 7.5, "funny", ("lol",))
    class Det:
        def __init__(self, stream, lag_s): self.fed, self.sent = [], False
        def feed(self, msg): self.fed.append(msg)
        def tick(self, now):
            if not self.sent:
                self.sent = True; return ev
    monkeypatch.setattr(run, "Detector", Det)
    cuts = []
    def fake_cut(buf, start, end, out, ffmpeg):
        cuts.append((start, end)); out.write_bytes(b"x"); return RawClip(out, start, end - start)
    monkeypatch.setattr(run, "cut", cut_fn or fake_cut)
    opened = []
    async def flaky_chat(cfg):
        opened.append(1)
        if len(opened) == 1: raise RuntimeError("socket died")
        if False: yield
    polls = iter([None] * 30 + [1])
    class Live(Proc):
        def poll(self): return next(polls, 1)
    async def stop(n): raise asyncio.CancelledError
    s = Settings(streams=(cfg,), data_dir=tmp_path, volume=tmp_path, narrative=False)
    db = open_db(tmp_path / "a.db")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stream_task(cfg, s, db, spawn=lambda c, s: Live(), chat=flaky_chat, sleep=stop))
    return cuts, opened, db


def test_closed_event_becomes_a_clip_and_chat_reconnects(tmp_path, monkeypatch):
    cuts, opened, db = _one_event(tmp_path, monkeypatch, StreamCfg("twitch", "a"))
    assert cuts == [(910.0, 1040.0)] and len(opened) >= 2           # round 3: 90 s before the onset, 30 s after
    row = db.execute("select * from clips").fetchone()
    assert row["state"] == "cut" and row["onset"] == 1000.0 and Path(row["raw_path"]).exists()
    assert json.loads(flag(db, "stream:twitch:a"))["capture"] is False


GOOD = {"title": "Kai loses it over a pizza order", "hook": "HE ORDERED WHAT",
        "description": "Kai reads the delivery order out loud and chat cannot handle it.",
        "tags": ["kai cenat", "funny"], "thumb_index": 1}


@pytest.fixture
def clip(tmp_path, monkeypatch):
    """A cut clip and fakes for every external step; returns (settings, db, clip id, raw path, calls)."""
    raw = tmp_path / "clips" / "c1" / "raw.ts"; raw.parent.mkdir(parents=True); raw.write_bytes(b"raw")
    s = Settings(streams=(StreamCfg("twitch", "kaicenat", name="Kai Cenat"),), data_dir=tmp_path, volume=tmp_path,
                 narrative=False)
    db = open_db(tmp_path / "a.db")
    cid = add_clip(db, "twitch:kaicenat", 1000.0, 1010.0, 9.0, "funny", ["LUL"], str(raw), 970.0)
    calls = {}
    def transcribe(media, ff, **kwargs):
        calls["transcribed"] = True; return [Word(30.0, 30.4, "pizza")]
    monkeypatch.setattr(run, "transcribe", transcribe)
    monkeypatch.setattr(run, "notify", lambda text: calls.setdefault("notes", []).append(text))
    monkeypatch.setattr(run, "_duration", lambda media, ff: 52.0)
    def frames(raw, a, b, n, d, ff):
        for i in range(n): (d / f"frame_{i}.jpg").write_bytes(b"j")
        return [d / f"frame_{i}.jpg" for i in range(n)]
    monkeypatch.setattr(run, "frames", frames)
    monkeypatch.setattr(run, "quality_check", lambda raw, ff, duration: (0.0, 0.0))
    (raw.parent / "context.json").write_text(json.dumps({"game": "Grand Theft Auto VI", "name": "",
                                                         "chat": [{"s": 30, "n": 40, "top": ["lol"]}]}))
    def edit(db, day, streamer, game, platform, mood, words, chat, onset_s, raw_s, cwd=None):
        calls["edit"] = (streamer, game, chat, onset_s, raw_s, cwd)
        out = {**GOOD, "keep": True, "what_happened": "Kai reads a wild pizza order out loud.", "start": 22.0, "end": 44.0}
        fake = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")
        return make_edit(db, day, streamer, game, platform, mood, words, chat, onset_s, raw_s, run=fake, cwd=cwd)
    monkeypatch.setattr(run, "make_edit", edit)
    def overlay(w, a, b, hook, d, layout=None, style="stroke"):
        calls["overlay_style"] = style
        d.mkdir(); (d / "subs.ffconcat").write_text("x"); return d / "subs.ffconcat"
    monkeypatch.setattr(run, "build_overlay", overlay)
    def render(raw, a, b, overlay, out, ff):
        calls["render"] = (a, b); out.write_bytes(b"mp4"); return out
    monkeypatch.setattr(run, "render_short", render)
    def thumb(raw, at, hook, out, ff):
        calls["thumb_at"] = at; out.write_bytes(b"t"); return out
    monkeypatch.setattr(run, "thumbnail", thumb)
    monkeypatch.setattr(run, "client", lambda profile: ("yt", f"UC-{profile}"))
    monkeypatch.setattr(run, "verified_channel", lambda profile: f"UC-{profile}", raising=False)
    def upload(yt, video, meta, credit, privacy):
        calls["upload"] = (meta.title, credit, privacy); return "vid1", privacy
    monkeypatch.setattr(run, "upload_short", upload)
    monkeypatch.setattr(run, "set_thumbnail", lambda yt, vid, jpg: True)
    monkeypatch.setattr(run, "wait_processed", lambda yt, vid: True)
    @contextlib.contextmanager
    def ctx(profile):
        calls["studio_profile"] = profile; yield "ctx"
    monkeypatch.setattr(run.studio, "context", ctx)
    def studio_upload(c, video, meta, credit, privacy, *, channel_id=None):
        calls["studio_uploads"] = calls.get("studio_uploads", 0) + 1; calls["studio_channel"] = channel_id
        return "sid1"
    monkeypatch.setattr(run.studio, "studio_upload", studio_upload)
    monkeypatch.setattr(run.studio, "studio_set_thumbnail", lambda c, vid, jpg: calls.setdefault("studio_thumb", vid))
    return s, db, cid, raw, calls


def _row(db, cid):
    return db.execute("select * from clips where id = ?", (cid,)).fetchone()


def test_process_clip_end_to_end(clip):
    s, db, cid, raw, calls = clip
    process_clip(db, s, _row(db, cid))
    row = _row(db, cid)
    assert row["state"] == "uploaded" and row["video_id"] == "vid1" and row["provider"] == "haiku"
    # the editor gets the streamer, game, chat timeline, onset and raw length, and its window is what gets rendered
    assert calls["edit"] == ("Kai Cenat", "Grand Theft Auto VI", ["30: 40 msgs | lol"], 30.0, 52.0, s.data_dir)
    assert calls["upload"] == (GOOD["title"], "Clipped from Kai Cenat's live stream: https://twitch.tv/kaicenat", "public")
    assert calls["render"] == (22.0, 44.0) and calls["thumb_at"] == 31.0      # thumbnail just after the onset
    assert "studio_profile" not in calls


def test_quota_exhausted_falls_back_to_studio(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def quota(*a): raise QuotaExhausted()
    monkeypatch.setattr(run, "upload_short", quota)
    process_clip(db, s, _row(db, cid))
    row = _row(db, cid)
    assert row["state"] == "uploaded" and row["video_id"] == "sid1"
    assert calls["studio_profile"] == s.data_dir / "chrome-profile" and calls["studio_thumb"] == "sid1"
    assert calls["studio_channel"] == "UC-primary"                     # primary's channel is verified too


def test_refused_thumbnail_goes_through_studio(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    monkeypatch.setattr(run, "set_thumbnail", lambda yt, vid, jpg: False)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["video_id"] == "vid1" and calls["studio_thumb"] == "vid1"


def test_upload_limit_pauses_and_puts_the_clip_back(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def limit(*a): raise UploadLimit()
    monkeypatch.setattr(run, "upload_short", limit)
    monkeypatch.setattr(run.time, "time", lambda: 5000.0)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and paused(db, "primary") == "limit:15800"


def test_any_other_error_fails_the_clip_and_deletes_nothing(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def boom(*a): raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(run, "render_short", boom)
    process_clip(db, s, _row(db, cid))
    row = _row(db, cid)
    assert row["state"] == "failed" and "ffmpeg exploded" in row["error"] and raw.exists()


def test_guard_lifts_an_expired_limit_pause(tmp_path):
    db = open_db(tmp_path / "a.db"); pause(db, "primary", "limit:1")
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    asyncio.run(guard_once(s, db, check=lambda ids, profile: [], notify=print))
    assert paused(db, "primary") == ""


def _toml(tmp_path):
    (tmp_path / "s.toml").write_text(f'data_dir="{tmp_path}"\nvolume="{tmp_path}"\n'
                                     '[[stream]]\nplatform="twitch"\nchannel="a"\n')
    return tmp_path / "s.toml"


class FakePopen:
    made = []
    def __init__(self, argv, **kw): self.argv, self.ended = argv, False; FakePopen.made.append(self)
    def terminate(self): self.ended = True
    def wait(self, timeout=None): return 0
    def poll(self): return 0 if self.ended else None


def test_main_recovers_an_interrupted_upload_before_starting(tmp_path, monkeypatch):
    _toml(tmp_path)
    monkeypatch.setattr(run.subprocess, "Popen", FakePopen); FakePopen.made = []
    db = open_db(tmp_path / "autoclipper.db")
    cid = add_clip(db, "twitch:a", 1.0, 2.0, 1.0, "funny", ["LUL"], "/x/r.ts", 0.0)
    set_state(db, cid, "uploading")
    async def crash(*a, **k): raise RuntimeError("stop here")
    monkeypatch.setattr(run, "stream_task", crash)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    assert _row(db, cid)["state"] == "failed"
    assert "recovered 1 interrupted clips" in (tmp_path / "logs" / "autoclipper.log").read_text()   # its own log file
    # C3: the daemon keeps the Mac awake through its own caffeinate child, stopped by handle on exit
    caf = [p for p in FakePopen.made if p.argv[0].endswith("caffeinate")]
    assert len(caf) == 1 and caf[0].argv[1:3] == ["-is", "-w"] and caf[0].ended


# --- audit fixes (round 2b)

def test_second_instance_exits_and_touches_nothing(tmp_path, monkeypatch):
    import fcntl
    _toml(tmp_path)
    held = open(tmp_path / "autoclipper.lock", "w"); fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    async def must_not_run(*a, **k): raise AssertionError("second instance started work")
    monkeypatch.setattr(run, "stream_task", must_not_run)
    with pytest.raises(SystemExit, match="already running"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    assert not (tmp_path / "autoclipper.db").exists()
    held.close()


def test_upload_is_recorded_before_the_thumbnail_and_never_repeated(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def quota(*a): raise QuotaExhausted()                  # thumbnail call hits the quota after the upload
    monkeypatch.setattr(run, "set_thumbnail", quota)
    process_clip(db, s, _row(db, cid))
    row = _row(db, cid)
    assert row["state"] == "uploaded" and row["video_id"] == "vid1" and "thumbnail" in row["error"]
    assert calls.get("studio_uploads", 0) == 0


def test_studio_thumbnail_failure_keeps_the_upload(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    monkeypatch.setattr(run, "set_thumbnail", lambda yt, vid, jpg: False)
    def not_logged_in(c, vid, jpg): raise RuntimeError("studio step 'thumbnail' timed out")
    monkeypatch.setattr(run.studio, "studio_set_thumbnail", not_logged_in)
    process_clip(db, s, _row(db, cid))
    row = _row(db, cid)
    assert row["state"] == "uploaded" and row["video_id"] == "vid1" and "timed out" in row["error"]


def test_no_api_client_pauses_before_any_work(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def unauthorised(profile): raise run.SignInError("run `autoclipper auth <client json>` first")
    monkeypatch.setattr(run, "client", unauthorised)
    process_clip(db, s, _row(db, cid)); process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and paused(db, "primary") == "auth"
    assert "transcribed" not in calls and "edit" not in calls and len(calls["notes"]) == 1


def test_pause_set_during_render_stops_the_upload(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def render(raw_, a, b, overlay, out, ff):
        pause(db, "primary", "strike:x"); out.write_bytes(b"mp4"); return out   # the guard fired meanwhile
    monkeypatch.setattr(run, "render_short", render)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and "upload" not in calls and (raw.parent / "short.mp4").exists()


def test_shutdown_returns_the_clip_to_cut(clip):
    s, db, cid, raw, calls = clip
    run.STOP.set()
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and "transcribed" not in calls


def test_every_ffmpeg_call_has_a_timeout(tmp_path, monkeypatch):
    import autoclipper.render as render
    seen = []
    def rec(argv, **kw):
        seen.append((Path(argv[0]).name, kw.get("timeout")))
        return subprocess.CompletedProcess(argv, 0, stdout="12.0", stderr="")
    monkeypatch.setattr(render.subprocess, "run", rec); monkeypatch.setattr(run.subprocess, "run", rec)
    raw = tmp_path / "raw.ts"; raw.write_bytes(b"x")
    render.frames(raw, 0.0, 4.0, 2, tmp_path, "ffmpeg")
    render.render_short(raw, 0.0, 4.0, tmp_path / "subs.ffconcat", tmp_path / "o.mp4", "ffmpeg")
    render.thumbnail(raw, 1.0, "HOOK", tmp_path / "t.jpg", "ffmpeg")
    run._duration(raw, "/opt/homebrew/bin/ffmpeg")
    assert len(seen) == 5 and all(t for _, t in seen)
    assert seen[2][1] == 600 and all(t == 120 for i, (_, t) in enumerate(seen) if i != 2)


def test_slot_waits_for_the_next_boundary_across_restarts(tmp_path):
    db, ids = _db(tmp_path, [5.0, 6.0]); done = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    def process(db, s, row):
        done.append(row["id"]); set_state(db, row["id"], "failed")      # "dropped" would move on to the next candidate
    assert asyncio.run(slot_once(s, db, process=process, now=lambda: 1000.0)) == ids[1]
    db = open_db(tmp_path / "a.db")                                       # a restart in the same slot
    assert asyncio.run(slot_once(s, db, process=process, now=lambda: 1060.0)) is None
    assert run.slot_wait(s, db, now=1300.0) == 1500.0                       # next boundary, not 30 min from now
    assert asyncio.run(slot_once(s, db, process=process, now=lambda: 2800.0)) == ids[0] and done == ids[::-1]


def test_expiry_runs_even_while_paused(tmp_path):
    raw = tmp_path / "clips" / "old" / "raw.ts"; raw.parent.mkdir(parents=True); raw.write_bytes(b"x")
    db = open_db(tmp_path / "a.db")
    cid = add_clip(db, "twitch:a", 10.0, 20.0, 1.0, "funny", ["LUL"], str(raw), 0.0)
    pause(db, "primary", "strike:v")
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    db.execute("update clips set created = 0 where id = ?", (cid,))       # queued long ago
    assert asyncio.run(slot_once(s, db, process=lambda *a: None, now=lambda: 9000.0)) is None
    assert _row(db, cid)["state"] == "dropped" and not raw.exists()


def test_uploaded_clip_keeps_only_the_short_and_thumbnail(clip):
    s, db, cid, raw, calls = clip
    process_clip(db, s, _row(db, cid))
    # the small analysis and chat files stay too: they are what the Jev weights get fitted against later
    assert sorted(p.name for p in raw.parent.iterdir()) == ["analysis.json", "context.json", "short.mp4", "thumb.jpg"]


def test_guard_sweeps_old_clip_directories_inside_clips_only(tmp_path):
    import os, time as _t
    clips = tmp_path / "clips"; db = open_db(tmp_path / "a.db")
    def make(name, state=None, old=True):
        d = clips / name; d.mkdir(parents=True); (d / "short.mp4").write_bytes(b"x")
        if state:
            cid = add_clip(db, "twitch:a", 1.0, 2.0, 1.0, "funny", ["L"], str(d / "raw.ts"), 0.0)
            set_state(db, cid, state)
        if old: os.utime(d, (1, 1))
        return d
    gone = [make("up", "uploaded"), make("orphan"), make("bad", "failed")]
    kept = [make("waiting", "cut"), make("fresh", "uploaded", old=False)]
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "keep.txt").write_text("x")
    (clips / "link").symlink_to(outside, target_is_directory=True)
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    asyncio.run(guard_once(s, db, check=lambda ids, profile: [], notify=print))
    assert not any(d.exists() for d in gone) and all(d.exists() for d in kept)
    assert (outside / "keep.txt").exists()


def test_no_cut_and_no_slot_when_the_disk_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("autoclipper.run.disk_ok", lambda s: False)
    cut_calls = []
    monkeypatch.setattr(run, "cut", lambda *a: cut_calls.append(a))
    monkeypatch.setattr(run, "CUT_DELAY_S", 0)
    s = Settings(streams=(StreamCfg("twitch", "a"),), data_dir=tmp_path, volume=tmp_path)
    db, ids = _db(tmp_path, [5.0])
    asyncio.run(run._cut_later(s.streams[0], s, db, tmp_path / "buf", Event("twitch:a", 1000.0, 1010.0, 5.0, 7.5, "funny", ()),
                               {"pending": 1, "chat": {}}))
    assert cut_calls == [] and not (tmp_path / "clips").exists()
    done = []
    assert asyncio.run(slot_once(s, db, process=lambda *a: done.append(1), now=lambda: 1000.0)) is None and done == []


def test_a_failing_capture_cycle_backs_off_instead_of_ending(tmp_path, monkeypatch):
    slept = []
    def spawn(cfg, s): raise OSError("streamlink missing")
    async def sleep(n):
        slept.append(n)
        if len(slept) == 3: raise asyncio.CancelledError
    s = Settings(streams=(StreamCfg("twitch", "a"),), data_dir=tmp_path, volume=tmp_path)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stream_task(s.streams[0], s, open_db(tmp_path / "a.db"), spawn=spawn, chat=no_chat, sleep=sleep))
    assert slept == [5, 10, 20]


def test_a_failed_cut_is_logged(tmp_path, monkeypatch, caplog):
    def broken(*a): raise RuntimeError("concat exploded")
    _one_event(tmp_path, monkeypatch, StreamCfg("twitch", "a"), cut_fn=broken)
    ours = [r for r in caplog.records if r.name == "autoclipper" and "cut failed" in r.getMessage()]
    assert ours and "concat exploded" in (ours[0].exc_text or "")


def test_spawn_capture_stops_streamlink_when_ffmpeg_fails(tmp_path, monkeypatch):
    class SL(FakePopen):
        stdout = type("Pipe", (), {"close": lambda self: None})()
    made = []
    def popen(argv, **kw):
        if made: raise OSError("no ffmpeg")
        made.append(SL(argv)); return made[0]
    monkeypatch.setattr(run.subprocess, "Popen", popen)
    s = Settings(streams=(StreamCfg("twitch", "a"),), data_dir=tmp_path, volume=tmp_path)
    with pytest.raises(OSError):
        run.spawn_capture(s.streams[0], s)
    assert made[0].ended


def test_slow_capture_shutdown_does_not_block_other_streams(tmp_path):
    import time as _t
    class Slow(Proc):
        def terminate(self): _t.sleep(0.3)
    ticks = []
    async def counter():
        while True:
            ticks.append(1); await asyncio.sleep(0.01)
    async def stop(n): raise asyncio.CancelledError
    async def go():
        c = asyncio.create_task(counter())
        s = Settings(streams=(StreamCfg("twitch", "a"),), data_dir=tmp_path, volume=tmp_path)
        try:
            await stream_task(s.streams[0], s, open_db(tmp_path / "a.db"), spawn=lambda c, s: Slow(), chat=no_chat, sleep=stop)
        finally:
            c.cancel()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    assert len(ticks) > 10


def test_pre_roll_above_the_editor_window_is_cut(tmp_path, monkeypatch):
    cuts, _, _ = _one_event(tmp_path, monkeypatch, StreamCfg("twitch", "a", pre_roll_s=100))
    assert cuts == [(895.0, 1040.0)]


def test_a_network_failure_does_not_pause_uploads(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def offline(profile): raise run.TransportError("no route to host")
    monkeypatch.setattr(run, "client", offline)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and not paused(db, "primary") and "edit" not in calls


def test_a_video_still_processing_gets_its_thumbnail_through_studio(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    monkeypatch.setattr(run, "wait_processed", lambda yt, vid: False)
    monkeypatch.setattr(run, "set_thumbnail", lambda *a: calls.setdefault("api_thumb", True))
    process_clip(db, s, _row(db, cid))
    assert "api_thumb" not in calls and calls["studio_thumb"] == "vid1" and _row(db, cid)["state"] == "uploaded"


# --- round 3: discovery, ranking, fast lane, editor and saving without uploads

from autoclipper.config import Discover
from autoclipper.discover import Live
from autoclipper.run import Watch, allowed, plan_slots


def _d(**kw):
    base = dict(game="Grand Theft Auto VI", min_viewers=500,
                pin=(StreamCfg("youtube", "@IShowSpeed"), StreamCfg("twitch", "kaicenat")),
                prefer=(StreamCfg("kick", "xqc"), StreamCfg("twitch", "xqc"), StreamCfg("twitch", "jynxzi"),
                        StreamCfg("kick", "adinross")))
    return Discover(**{**base, **kw})


def _live(key, viewers):
    platform, channel = key.split(":")
    return Live(platform, channel, channel.title(), viewers, "Grand Theft Auto VI")


def _found(key, viewers=1000, busy=False, low=0):
    platform, channel = key.split(":")
    return Watch(StreamCfg(platform, channel), "found", low, viewers, busy)


def test_pins_then_preferred_any_game_then_the_biggest_in_the_game():
    listing = {k: _live(k, v) for k, v in [("twitch:big", 9000), ("kick:mid", 3000), ("twitch:small", 400)]}
    plan = plan_slots({}, _d(), listing, {"twitch", "kick"}, {"twitch:kaicenat", "kick:xqc", "twitch:xqc"}, 5)
    # kick:xqc and twitch:xqc are one person: the first entry in the preferred list wins; small is under min_viewers
    assert [(k, w.kind) for k, w in plan.items()] == [("twitch:kaicenat", "pin"), ("kick:xqc", "prefer"),
                                                      ("twitch:big", "found"), ("kick:mid", "found")]


def test_a_busy_discovered_stream_is_not_swapped_for_a_preferred_one():
    watched = {"twitch:a": _found("twitch:a", busy=True), "twitch:b": _found("twitch:b")}
    listing = {k: _live(k, 2000) for k in ("twitch:a", "twitch:b")}
    plan = plan_slots(watched, _d(), listing, {"twitch", "kick"}, {"kick:xqc"}, 2)
    assert set(plan) == {"twitch:a", "kick:xqc"}                     # b (not busy) gave its slot to the preferred one
    plan = plan_slots(watched, _d(), listing, {"twitch", "kick"}, {"kick:xqc", "twitch:kaicenat"}, 2)
    assert set(plan) == {"twitch:kaicenat", "twitch:a"}            # pinned beats preferred; busy a keeps its slot


def test_a_pin_waits_for_a_busy_slot_and_never_wins():
    watched = {"twitch:a": _found("twitch:a", busy=True)}
    live = {"twitch:a": _live("twitch:a", 900)}
    plan = plan_slots(watched, _d(), live, {"twitch"}, {"youtube:@IShowSpeed"}, 1)
    assert list(plan) == ["twitch:a"]                              # mid-event: the pin waits for its cut
    watched["twitch:a"].busy = False
    plan = plan_slots(watched, _d(never=(StreamCfg("youtube", "@IShowSpeed"),)), live, {"twitch"},
                      {"youtube:@IShowSpeed", "twitch:kaicenat"}, 1)
    assert list(plan) == ["twitch:kaicenat"]                       # the never list beats even a pin


def test_discovered_streams_leave_when_offline_or_low_twice_and_stay_when_the_listing_failed():
    watched = {"twitch:gone": _found("twitch:gone"), "twitch:low": _found("twitch:low", low=1),
               "twitch:dip": _found("twitch:dip"), "kick:k": _found("kick:k")}
    listing = {"twitch:low": _live("twitch:low", 100), "twitch:dip": _live("twitch:dip", 100)}
    plan = plan_slots(watched, _d(pin=(), prefer=()), listing, {"twitch"}, set(), 5)
    assert set(plan) == {"twitch:dip", "kick:k"} and plan["twitch:dip"].low == 1


def test_allowed_skips_the_never_list_and_streams_without_recent_clips(caplog):
    caplog.set_level("INFO", logger="autoclipper")
    run._CLIPS_OK.clear()
    listing = {k: _live(k, v) for k, v in [("twitch:open", 2000), ("kick:closed", 1500), ("twitch:no", 1200),
                                           ("twitch:tiny", 10)]}
    checked = []
    def check(live):
        checked.append(live.channel); return live.channel == "open"
    d = _d(never=(StreamCfg("twitch", "no"),))
    # only streams big enough to be picked are eligible; whether a watched one is still live comes from the raw listing
    assert set(allowed(listing, d, check=check, now=lambda: 1000.0)) == {"twitch:open"}
    assert set(allowed(listing, d, check=check, now=lambda: 2000.0)) == {"twitch:open"}
    assert checked == ["open", "closed"]                              # cached for 24 h; tiny is never checked
    assert "skip kick:closed (no clip in the last 7 days" in caplog.text and "skip twitch:no (never list)" in caplog.text


def test_discovery_starts_and_stops_streams_by_handle(tmp_path):
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, discover=_d(pin=(), prefer=()))
    listings = iter([{"twitch:a": _live("twitch:a", 900), "twitch:b": _live("twitch:b", 800)},
                     {"twitch:b": _live("twitch:b", 800)}])
    started, cancelled = [], []
    async def run_stream(cfg, state):
        started.append((cfg.key, state["game"]))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(cfg.key); raise
    naps = []
    async def sleep(n):
        naps.append(n); await asyncio.sleep(0.01)
        if len(naps) == 2: raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run.discover_loop(s, open_db(tmp_path / "a.db"), lister=lambda d: (next(listings), {"twitch"}),
                                      allow=lambda listing, d: listing, probe=lambda url: (False, None),
                                      run_stream=run_stream, sleep=sleep))
    assert started == [("twitch:a", "Grand Theft Auto VI"), ("twitch:b", "Grand Theft Auto VI")]
    assert cancelled[0] == "twitch:a" and sorted(cancelled) == ["twitch:a", "twitch:b"] and naps[0] == 300


@pytest.fixture
def candidates(tmp_path, monkeypatch):
    """Three cut clips with raw files and chat context; preparation and Jev are faked per test."""
    s = Settings(streams=(StreamCfg("twitch", "a"),), data_dir=tmp_path, volume=tmp_path)
    db = open_db(tmp_path / "a.db"); ids = []
    for i, score in enumerate([5.0, 9.0, 7.0]):
        raw = tmp_path / "clips" / f"c{i}" / "raw.ts"; raw.parent.mkdir(parents=True); raw.write_bytes(b"x")
        (raw.parent / "context.json").write_text(json.dumps({"game": "G", "name": "", "chat": []}))
        ids.append(add_clip(db, "twitch:a", 1000.0 + i, 1010.0 + i, score, "funny", ["LUL"], str(raw), 910.0))
    monkeypatch.setattr(run, "transcribe", lambda media, ff, **kwargs: [Word(1.0, 1.2, "yo")])
    monkeypatch.setattr(run, "_duration", lambda media, ff: 130.0)
    monkeypatch.setattr(run, "frames", lambda raw, a, b, n, d, ff: [d / f"frame_{k}.jpg" for k in range(n)])
    monkeypatch.setattr(run, "quality_check", lambda raw, ff, duration: (0.0, 0.0))
    monkeypatch.setitem(run.SECRETS, "jev", "key")
    return s, db, ids


def _jev(ranks):
    """A fake jev.ask answering each clip (by its onset second in the state) with all scores equal to ranks[i]."""
    def ask(body, key):
        i = round(body["state"]["chat_spike_starts_at_second"]) - 90                      # 90 s into each raw cut
        v = ranks[i]
        answers = {q: ({"type": "noul", "noul": 0.0} if spec["type"] == "noul" else
                       {"type": "score", "score": v * (len(spec["criteria"]) - 1)}) for q, spec in run.jev.QUESTIONS.items()}
        return {"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 5000, "output_tokens": 9}}
    return ask


def test_jev_ranks_the_candidates_and_drops_weak_and_frozen_ones(candidates, monkeypatch):
    s, db, ids = candidates
    monkeypatch.setattr(run.jev, "ask", _jev([0.9, 0.3, 0.6]))
    monkeypatch.setattr(run, "quality_check", lambda raw, ff, d: (0.9, 0.0) if "c2" in str(raw) else (0.0, 0.0))
    rows = run.cut_rows(db, 1100.0, 3600)
    assert [r["id"] for r in ORIGINAL_CHOOSE(db, s, rows)] == [ids[0]]   # c1 under MIN_RANK, c2 mostly frozen
    assert _row(db, ids[2])["state"] == "dropped" and "frozen" in _row(db, ids[2])["error"]
    got = run.get_scores(db, ids[0])
    assert abs(got["rank"] - 0.9) < 1e-9 and got["input_tokens"] == 5000 and "hook" in json.loads(got["answers"])["scores"]


def test_without_a_jev_key_the_detector_score_decides(candidates, monkeypatch):
    s, db, ids = candidates
    monkeypatch.setitem(run.SECRETS, "jev", None)
    monkeypatch.setattr(run.jev, "ask", lambda body, key: pytest.fail("no key: Jev is never asked"))
    rows = run.cut_rows(db, 1100.0, 3600)
    assert [r["id"] for r in ORIGINAL_CHOOSE(db, s, rows)] == [ids[1], ids[2], ids[0]]


def test_a_jev_failure_posts_nothing_since_jev_is_the_nudity_gate(candidates, monkeypatch):
    s, db, ids = candidates
    def down(body, key): raise run.jev.JevError("529 after retries")
    monkeypatch.setattr(run.jev, "ask", down)
    rows = run.cut_rows(db, 1100.0, 3600)
    assert ORIGINAL_CHOOSE(db, s, rows) == []                              # no frame check: the slot waits
    assert all(_row(db, i)["state"] == "cut" for i in ids)                 # nothing dropped; they wait for Jev


def test_fast_lane_posts_a_strong_clip_at_once_but_keeps_the_gap(candidates, monkeypatch):
    s, db, ids = candidates
    monkeypatch.setattr(run.jev, "ask", _jev([0.85, 0.5, 0.95]))
    done = []
    def process(db, s, row):
        done.append(row["id"]); set_state(db, row["id"], "uploaded", video_id="v", uploaded_at=1100.0)
    assert run.prep_once(db, s, process=process, now=lambda: 1100.0) == ids[1 + 1]   # first strong one in score order
    assert run.prep_once(db, s, process=process, now=lambda: 1200.0) is None          # c0 is strong, but 5 min have not passed
    assert run.prep_once(db, s, process=process, now=lambda: 1400.0) == ids[0] and done == [ids[2], ids[0]]
    assert all(run.get_scores(db, i) is not None for i in ids)                       # every cut prepared and scored


def test_fast_lane_moves_on_when_a_clip_must_wait_for_its_channel(candidates, monkeypatch):
    s, db, ids = candidates
    monkeypatch.setattr(run.jev, "ask", _jev([0.85, 0.5, 0.95]))
    done = []
    def process(db, s, row):
        done.append(row["id"])
        if row["id"] == ids[2]:                    # its channel cannot sign in: back to cut, nothing paused
            set_state(db, row["id"], "cut"); return run.WAITS
        set_state(db, row["id"], "uploaded", video_id="v", uploaded_at=1100.0)
    assert run.prep_once(db, s, process=process, now=lambda: 1100.0) == ids[0]
    assert done == [ids[2], ids[0]]


def test_cut_writes_the_chat_timeline_of_its_window(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "CUT_DELAY_S", 0)
    def fake_cut(buf, start, end, out, ff):
        out.write_bytes(b"x"); return RawClip(out, 910.0, 130.0)
    monkeypatch.setattr(run, "cut", fake_cut)
    s = Settings(streams=(StreamCfg("twitch", "a"),), data_dir=tmp_path, volume=tmp_path)
    db = open_db(tmp_path / "a.db")
    chat = {900: [3, Counter({"x": 3})], 1001: [40, Counter({"lol": 30, "w": 5, "kekw": 4, "omg": 1})]}
    state = {"pending": 1, "chat": chat, "game": "Grand Theft Auto VI"}
    asyncio.run(run._cut_later(s.streams[0], s, db, tmp_path / "buf",
                               Event("twitch:a", 1000.0, 1010.0, 5.0, 7.5, "funny", ()), state))
    ctx = json.loads((tmp_path / "clips" / "twitch-a-1000" / "context.json").read_text())
    assert ctx["game"] == "Grand Theft Auto VI" and ctx["chat"] == [{"s": 91, "n": 40, "top": ["lol", "w", "kekw"]}]
    assert state["pending"] == 0


def test_a_stopped_stream_keeps_its_buffer_until_its_cut_ran_then_drops_it(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "TICK_S", 0.01); monkeypatch.setattr(run, "CUT_DELAY_S", 0.2)
    cuts = []
    def fake_cut(buf, start, end, out, ff):                   # capture stopped before the tail: the real cut fails
        cuts.append((start, buf.is_dir())); return None
    monkeypatch.setattr(run, "cut", fake_cut)
    ev = Event("twitch:a", 1000.0, 1010.0, 5.0, 7.5, "funny", ())
    class Det:
        def __init__(self, stream, lag_s): self.onset, self.sent = None, False
        def feed(self, msg): pass
        def tick(self, now):
            if not self.sent:
                self.sent = True; return ev
    monkeypatch.setattr(run, "Detector", Det)
    s = Settings(streams=(StreamCfg("twitch", "a"),), data_dir=tmp_path, volume=tmp_path, narrative=False)
    async def go():
        t = asyncio.create_task(stream_task(s.streams[0], s, open_db(tmp_path / "a.db"),
                                            spawn=lambda c, s: Proc(alive=True), chat=no_chat))
        await asyncio.sleep(0.05); t.cancel()                 # the stream ends mid-cut (offline)
        await asyncio.gather(t, return_exceptions=True)
        assert (tmp_path / "buf" / "twitch-a").is_dir()     # kept while its cut is pending
        await asyncio.sleep(0.4)
    asyncio.run(go())
    assert cuts == [(910.0, True)] and not (tmp_path / "buf" / "twitch-a").exists()


def test_uploads_off_saves_the_finished_short_with_its_metadata(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    s = Settings(streams=s.streams, data_dir=s.data_dir, volume=s.volume, upload=False, narrative=False)
    def no_youtube(): raise AssertionError("YouTube must not be touched with uploads off")
    monkeypatch.setattr(run, "client", no_youtube)
    pause(db, "primary", "auth")                                # a YouTube pause does not stop saving
    process_clip(db, s, _row(db, cid))
    row = _row(db, cid)
    assert row["state"] == "saved" and row["title"] == GOOD["title"] and row["provider"] == "haiku"
    meta = json.loads((raw.parent / "meta.json").read_text())
    assert meta["what_happened"] == "Kai reads a wild pizza order out loud." and meta["url"] == "https://twitch.tv/kaicenat"
    assert "GTA 6" in meta["tags"] and meta["window"] == [22.0, 44.0] and "upload" not in calls
    assert {"short.mp4", "thumb.jpg", "meta.json"} <= {p.name for p in raw.parent.iterdir()} and not raw.exists()


def test_sigterm_stops_the_daemon_quietly(tmp_path, monkeypatch):
    import os, signal
    _toml(tmp_path)
    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    async def forever(*a, **k): await asyncio.Event().wait()
    monkeypatch.setattr(run, "stream_task", forever)
    async def go():
        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        await run.main(tmp_path / "s.toml")                 # launchd's stop: no CancelledError traceback in the log
    asyncio.run(go())
    assert run.STOP.is_set() and "stopped by signal" in (tmp_path / "logs" / "autoclipper.log").read_text()


# --- round 3 audit fixes

def test_a_slot_never_processes_a_clip_the_fast_lane_just_posted(tmp_path):
    db, ids = _db(tmp_path, [5.0]); done = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    async def go():
        run.HEAVY.acquire()                                    # the fast lane is mid-upload
        task = asyncio.create_task(slot_once(s, db, process=lambda db, s, row: done.append(row["id"]), now=lambda: 1000.0))
        await asyncio.sleep(0.05)
        set_state(db, ids[0], "uploaded", video_id="v", uploaded_at=999.0)
        run.HEAVY.release()
        return await task
    assert asyncio.run(go()) is None and done == [] and _row(db, ids[0])["state"] == "uploaded"


def test_slots_keep_the_minimum_gap_too(tmp_path):
    db, ids = _db(tmp_path, [5.0, 6.0]); done = []
    set_state(db, ids[0], "uploaded", video_id="v", uploaded_at=900.0)
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, min_gap_minutes=5)
    assert asyncio.run(slot_once(s, db, process=lambda db, s, row: done.append(1), now=lambda: 1000.0)) is None
    assert done == []


def test_secrets_are_read_once_at_startup_with_a_timeout(tmp_path, monkeypatch, caplog):
    import time as _t
    _toml(tmp_path)
    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(run, "SECRET_TIMEOUT_S", 0.2)
    monkeypatch.setattr("keyring.get_password", lambda service, name: _t.sleep(5))      # a Keychain that hangs
    async def crash(*a, **k): raise RuntimeError("stop here")
    monkeypatch.setattr(run, "stream_task", crash)
    t0 = _t.monotonic()
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    assert _t.monotonic() - t0 < 3 and run.SECRETS == {"jev": None, "twitch": None, "ok": False}
    assert "Keychain did not answer" in (tmp_path / "logs" / "autoclipper.log").read_text()


def test_chat_history_expires_while_reading_even_without_capture():
    from autoclipper.chat import ChatMessage
    msgs = [ChatMessage(float(t), "u", "lol") for t in range(0, 2000, 10)]
    async def chat(cfg):
        for m in msgs:
            yield m
        await asyncio.Event().wait()
    state = {"det": None, "chat": {}, "last_chat": None}
    async def go():
        t = asyncio.create_task(run._read_chat(StreamCfg("twitch", "a"), chat, state))
        await asyncio.sleep(0.05); t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())
    assert min(state["chat"]) >= 1990 - run.CHAT_KEEP_S and max(state["chat"]) == 1990


def test_buffers_of_retired_streams_are_removed_and_live_ones_pruned(tmp_path):
    import os
    buf = tmp_path / "buf"
    def run_dir(stream, name, mtime):
        d = buf / stream / name; d.mkdir(parents=True)
        f = d / "00000000.ts"; f.write_bytes(b"x"); os.utime(f, (mtime, mtime))
        (d / "list.csv").write_text("00000000.ts,0.0,4.0\n")
        return d
    live_old, live_new = run_dir("twitch-live", "100", 100.0), run_dir("twitch-live", "990", 990.0)
    gone, waiting = run_dir("twitch-gone", "500", 500.0), run_dir("twitch-wait", "500", 500.0)
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "keep.ts").write_bytes(b"x")
    (buf / "linked").symlink_to(outside, target_is_directory=True)
    run.BUFS.update({"twitch-live": {"active": True, "pending": 0}, "twitch-wait": {"active": False, "pending": 1}})
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    run.sweep_buffers(s, now=1000.0)
    assert not (live_old / "00000000.ts").exists() and (live_new / "00000000.ts").exists()
    assert not (buf / "twitch-gone").exists() and waiting.is_dir() and (outside / "keep.ts").exists()


def test_uploads_off_never_asks_youtube_in_the_guard(tmp_path):
    db, ids = _db(tmp_path, [5.0]); set_state(db, ids[0], "uploaded", video_id="v1", uploaded_at=1.0)
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, upload=False)
    def no_youtube(ids, profile): raise AssertionError("strike check with uploads off")
    asyncio.run(guard_once(s, db, check=no_youtube, notify=print))


def test_liveness_uses_the_raw_listing_and_eligibility_only_picks_new_streams():
    watched = {"twitch:a": _found("twitch:a")}
    listing = {"twitch:a": _live("twitch:a", 900), "twitch:b": _live("twitch:b", 800), "twitch:c": _live("twitch:c", 700)}
    eligible = {"twitch:c": listing["twitch:c"]}                      # a's clip check failed, b has no recent clips
    plan = plan_slots(watched, _d(pin=(), prefer=()), listing, {"twitch"}, set(), 5, eligible=eligible)
    assert set(plan) == {"twitch:a", "twitch:c"}


def test_failed_clip_checks_are_cached_and_the_queue_moves_on(monkeypatch):
    run._CLIPS_OK.clear()
    monkeypatch.setattr(run, "CLIP_CHECKS_PER_REFRESH", 2)
    listing = {f"twitch:s{i}": _live(f"twitch:s{i}", 5000 - i) for i in range(5)}
    checked = []
    def check(live):
        checked.append(live.channel)
        if live.channel in ("s0", "s1"):
            raise RuntimeError("Helix 503")
        return True
    d = _d(pin=(), prefer=())
    assert allowed(listing, d, check=check, now=lambda: 1000.0) == {}
    assert set(allowed(listing, d, check=check, now=lambda: 1300.0)) == {"twitch:s2", "twitch:s3"}
    assert checked == ["s0", "s1", "s2", "s3"]                         # failures wait 10 min, the rest get their turn
    allowed(listing, d, check=check, now=lambda: 1700.0)
    assert checked[-2:] == ["s0", "s1"]                                # retried after 10 min


def test_retired_streams_finish_before_their_replacements_start(tmp_path):
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, discover=_d(pin=(), prefer=()))
    listings = iter([{f"twitch:a{i}": _live(f"twitch:a{i}", 900) for i in range(5)},
                     {f"twitch:b{i}": _live(f"twitch:b{i}", 900) for i in range(5)}])
    running, peak = set(), []
    async def run_stream(cfg, state):
        running.add(cfg.key); peak.append(len(running))
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)                          # a slow shutdown: capture terminating
            running.discard(cfg.key)
    naps = []
    async def sleep(n):
        naps.append(n); await asyncio.sleep(0.01)
        if len(naps) == 2: raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run.discover_loop(s, open_db(tmp_path / "a.db"), lister=lambda d: (next(listings), {"twitch"}),
                                      allow=lambda listing, d: listing, probe=lambda url: (False, None),
                                      run_stream=run_stream, sleep=sleep))
    assert max(peak) == 5


def test_fixed_streams_respect_the_never_list_and_one_person_once(tmp_path, caplog):
    caplog.set_level("INFO", logger="autoclipper")
    s = Settings(streams=(StreamCfg("twitch", "xqc"), StreamCfg("kick", "xqc"), StreamCfg("twitch", "banned")),
                 data_dir=tmp_path, volume=tmp_path, discover=_d(never=(StreamCfg("twitch", "banned"),)))
    assert [c.key for c in run.fixed_streams(s)] == ["twitch:xqc"]
    assert "twitch:banned" in caplog.text and "kick:xqc" in caplog.text
    plan = plan_slots({}, _d(pin=(), prefer=()), {"kick:xqc": _live("kick:xqc", 9000)}, {"kick"}, set(), 4,
                      taken=run.fixed_streams(s))
    assert plan == {}                                                 # xqc already watched as a fixed stream


def test_the_guard_retries_a_failed_keychain_read_until_it_answers(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="autoclipper")
    monkeypatch.setattr(run, "SECRETS", {"jev": None, "twitch": None, "ok": False})   # the startup read timed out
    monkeypatch.setattr(run, "_HELIX", [None])
    answers = iter([{"jev": None, "twitch": None, "ok": False}, {"jev": "key", "twitch": ("id", "secret"), "ok": True}])
    calls = []
    def read():
        calls.append(1); return next(answers)
    monkeypatch.setattr(run, "_read_secrets", read)
    db = open_db(tmp_path / "a.db")
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, upload=False)
    run.HEAVY.acquire()                                         # a long render holds HEAVY: the retry must not wait for it
    try:
        asyncio.run(asyncio.wait_for(guard_once(s, db, check=print, notify=print), 5))
        assert run.SECRETS["ok"] is False and run.SECRETS["jev"] is None
        asyncio.run(asyncio.wait_for(guard_once(s, db, check=print, notify=print), 5))
    finally:
        run.HEAVY.release()
    assert run.SECRETS == {"jev": "key", "twitch": ("id", "secret"), "ok": True} and run._HELIX == []
    assert "Jev ranking and Twitch discovery are back" in caplog.text and len(calls) == 2
    asyncio.run(guard_once(s, db, check=print, notify=print))
    assert len(calls) == 2                                      # once it answered, no more Keychain reads


def test_backup_upload_and_guard_use_the_selected_profile(clip, monkeypatch):
    from dataclasses import replace

    s, db, cid, raw, calls = clip
    s = replace(s, youtube_profile="backup")
    primary = add_clip(db, "twitch:a", 900.0, 920.0, 5.0, "funny", [], "/x/r.ts", 870.0)
    set_state(db, primary, "uploaded", video_id="primary-video", uploaded_at=1.0)
    opened = []
    monkeypatch.setattr(run, "client", lambda profile: opened.append(profile) or ("yt", f"UC-{profile}"))
    process_clip(db, s, _row(db, cid))

    assert opened == ["backup"]
    assert _row(db, cid)["youtube_profile"] == "backup"
    checked = []

    def check(ids, profile):
        checked.append((ids, profile))
        return []

    asyncio.run(guard_once(s, db, check=check))
    assert checked == [(["vid1"], "backup")]


def test_ineligible_candidate_does_not_spend_the_posting_slot(tmp_path, monkeypatch):
    db, ids = _db(tmp_path, [5.0])
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    monkeypatch.setattr(run, "choose_clips", lambda db, s, rows: [])
    assert asyncio.run(slot_once(s, db, now=lambda: 1000.0)) is None
    assert flag(db, "last_slot") is None

    monkeypatch.setattr(run, "choose_clips", lambda db, s, rows: rows)
    assert asyncio.run(slot_once(s, db, process=lambda *args: None, now=lambda: 1060.0)) == ids[0]
    assert flag(db, "last_slot") == "1060.0"


# --- the 16:9 landscape companion

@pytest.fixture
def paired(clip, monkeypatch):
    """The clip fixture with landscape = true, fake landscape renders and an uploader that records each call."""
    from dataclasses import replace
    s, db, cid, raw, calls = clip
    s = replace(s, landscape=True)
    def overlay(w, a, b, hook, d, layout=None, style="stroke"):
        d.mkdir(exist_ok=True); (d / "subs.ffconcat").write_text("x"); calls.setdefault("overlays", []).append((hook, layout))
        calls.setdefault("overlay_styles", []).append(style); return d / "subs.ffconcat"
    monkeypatch.setattr(run, "build_overlay", overlay)
    def render(raw_, a, b, overlay_, out, ff):
        calls["wide_render"] = (a, b); out.write_bytes(b"wide"); return out
    monkeypatch.setattr(run, "render_landscape", render)
    def thumb(raw_, at, hook, out, ff):
        calls["wide_thumb"] = (at, hook); out.write_bytes(b"t"); return out
    monkeypatch.setattr(run, "landscape_thumbnail", thumb)
    def upload(yt, video, meta, credit, privacy, *, short=True):
        calls.setdefault("uploads", []).append((video.name, short, privacy, meta.title))
        return ("vid1" if short else "wide1"), privacy
    monkeypatch.setattr(run, "upload_short", upload)
    # the transcript around the editor's 22..44 window: setup from 18.0, reaction until 47.0, then a pause
    words = [Word(18.0 + 0.3 * i, 18.25 + 0.3 * i, "w") for i in range(15)] + \
            [Word(44.5 + 0.3 * i, 44.75 + 0.3 * i, "w") for i in range(9)]
    monkeypatch.setattr(run, "transcribe", lambda media, ff, **kwargs: words)
    return s, db, cid, raw, calls


def _out(db, cid):
    from autoclipper.store import get_output
    return get_output(db, cid, "landscape")


LATER = lambda: run.time.time() + 3600            # past min_gap_minutes after the Short


def test_landscape_off_changes_nothing(clip):
    s, db, cid, raw, calls = clip
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded" and _out(db, cid) is None
    assert not (raw.parent / "meta.json").exists() and run.companion_once(db, s, now=LATER) is None


def test_the_companion_posts_after_the_short_with_the_gap_as_a_regular_video(paired):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded" and calls["uploads"] == [("short.mp4", True, "public", GOOD["title"])]
    out = _out(db, cid)
    assert out["state"] == "pending" and out["youtube_profile"] == "primary"
    start, end = calls["wide_render"]
    assert 17.7 <= start < 22.0 and 47.0 <= end < 48.0                 # a little setup and reaction, at pauses
    assert calls["overlays"][-1] == ("", run.WIDE) and calls["wide_thumb"] == (31.0, GOOD["hook"])
    assert run.companion_once(db, s) is None and len(calls["uploads"]) == 1   # min_gap_minutes after the Short
    assert run.companion_once(db, s, now=LATER) == cid
    assert calls["uploads"][-1] == ("landscape.mp4", False, "public", GOOD["title"])
    out = _out(db, cid)
    assert out["state"] == "uploaded" and out["video_id"] == "wide1"
    assert run.companion_once(db, s, now=LATER) is None and len(calls["uploads"]) == 2   # never twice
    meta = json.loads((raw.parent / "meta.json").read_text())["outputs"]
    assert meta["short"]["video_id"] == "vid1" and meta["short"]["description"].endswith("#shorts")
    assert meta["landscape"]["video_id"] == "wide1" and "#shorts" not in meta["landscape"]["description"]
    assert meta["landscape"]["title"] == GOOD["title"] and meta["landscape"]["thumb"].endswith("landscape.jpg")
    assert meta["landscape"]["window"] == [start, end]
    kept = {p.name for p in raw.parent.iterdir()}
    assert {"short.mp4", "thumb.jpg", "landscape.mp4", "landscape.jpg", "meta.json"} <= kept
    assert "raw.ts" not in kept and "overlay_wide" not in kept


def test_the_companion_counts_toward_the_daily_cap(paired):
    from dataclasses import replace
    s, db, cid, raw, calls = paired
    s = replace(s, max_uploads_per_day=1)
    process_clip(db, s, _row(db, cid))
    assert run.companion_once(db, s, now=LATER) is None and _out(db, cid)["state"] == "pending"
    assert len(calls["uploads"]) == 1


def test_a_failed_companion_keeps_the_short_and_is_not_retried(paired, monkeypatch):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    def broken(yt, video, meta, credit, privacy, *, short=True): raise RuntimeError("upload broke")
    monkeypatch.setattr(run, "upload_short", broken)
    assert run.companion_once(db, s, now=LATER) is None
    assert _out(db, cid)["state"] == "failed" and "upload broke" in _out(db, cid)["error"]
    assert _row(db, cid)["state"] == "uploaded" and _row(db, cid)["video_id"] == "vid1"
    assert run.companion_once(db, s, now=LATER) is None


def test_a_companion_render_failure_keeps_the_short(paired, monkeypatch):
    s, db, cid, raw, calls = paired
    def boom(*a): raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(run, "render_landscape", boom)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded" and _row(db, cid)["error"] is None
    assert _out(db, cid)["state"] == "failed" and "ffmpeg exploded" in _out(db, cid)["error"]


def test_an_interrupted_companion_upload_is_never_posted_again(paired, monkeypatch):
    from autoclipper.store import recover, set_output
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    set_output(db, cid, "landscape", "uploading")                 # the daemon died mid-upload
    recover(db)
    assert _out(db, cid)["state"] == "failed" and run.companion_once(db, s, now=LATER) is None
    assert len(calls["uploads"]) == 1


def test_an_upload_limit_puts_the_companion_back_and_pauses(paired, monkeypatch):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    def limit(*a, **k): raise UploadLimit()
    monkeypatch.setattr(run, "upload_short", limit)
    assert run.companion_once(db, s, now=LATER) is None
    assert _out(db, cid)["state"] == "pending" and paused(db, "primary").startswith("limit:")


def test_the_companion_falls_back_to_studio_without_the_shorts_marker(paired, monkeypatch):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    def quota(*a, **k): raise QuotaExhausted()
    monkeypatch.setattr(run, "upload_short", quota)
    seen = {}
    def studio_upload(c, video, meta, credit, privacy, *, channel_id=None, short=True):
        seen.update(video=video.name, short=short, channel=channel_id); return "swide"
    monkeypatch.setattr(run.studio, "studio_upload", studio_upload)
    assert run.companion_once(db, s, now=LATER) == cid
    assert seen == {"video": "landscape.mp4", "short": False, "channel": "UC-primary"}
    assert _out(db, cid)["video_id"] == "swide"
    assert calls["studio_thumb"] == "swide"


def test_the_companion_waits_for_its_own_profile(paired):
    from dataclasses import replace
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    assert run.companion_once(db, replace(s, youtube_profile="backup"), now=LATER) is None
    assert _out(db, cid)["state"] == "pending"


def test_uploads_off_saves_both_formats_for_review(paired, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = paired
    s = replace(s, upload=False)
    def no_youtube(profile): raise AssertionError("YouTube must not be touched with uploads off")
    monkeypatch.setattr(run, "client", no_youtube)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "saved" and _out(db, cid)["state"] == "saved" and "uploads" not in calls
    meta = json.loads((raw.parent / "meta.json").read_text())
    assert meta["title"] == GOOD["title"] and meta["window"] == [22.0, 44.0]       # the existing fields stay
    assert meta["outputs"]["short"]["video"].endswith("short.mp4") and meta["outputs"]["short"]["video_id"] is None
    assert meta["outputs"]["landscape"]["state"] == "saved" and (raw.parent / "landscape.mp4").exists()
    assert run.companion_once(db, s, now=LATER) is None


def test_a_waiting_companion_keeps_its_clip_folder(paired):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    import os
    os.utime(raw.parent, (1, 1))
    assert run.sweep_clips(s, db, run.time.time()) == 0 and raw.parent.exists()


def test_a_companion_older_than_the_retention_no_longer_keeps_its_folder(paired):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    import os
    os.utime(raw.parent, (1, 1))
    assert run.sweep_clips(s, db, run.time.time() + run.KEEP_CLIPS_S + 60) == 1 and not raw.parent.exists()


def test_the_companion_survives_a_restart_after_the_short_is_uploaded(paired, monkeypatch):
    from dataclasses import replace

    s, db, cid, raw, calls = paired

    def interrupted(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(run, "_thumb", interrupted)
    with pytest.raises(KeyboardInterrupt):
        process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["video_id"] == "vid1"
    assert _out(db, cid)["state"] == "pending"

    monkeypatch.setattr(run, "_thumb", lambda *args: None)
    assert run.companion_once(db, replace(s, privacy="private"), now=LATER) == cid
    assert calls["uploads"][-1][1:3] == (False, "public")


def test_story_mode_renders_the_full_event_and_its_nested_short(paired, monkeypatch):
    from dataclasses import replace
    from autoclipper.meta import parse_edit

    s, db, cid, raw, calls = paired
    s = replace(s, narrative=True)
    monkeypatch.setattr(run, "_duration", lambda *args: 130)
    answer = {
        **GOOD, "keep": True, "start": 10, "end": 45, "story_start": 2, "story_end": 82, "event_at": 26,
        "context": "Kai reads an unexpected pizza order aloud.",
        "what_happened": "Kai discovers an enormous pizza order.",
        "reaction": "Kai bursts out laughing at the size of the order.",
    }

    def choose(*args, narrative, **kwargs):
        assert narrative is True
        return parse_edit(json.dumps(answer), 130, 30, "haiku", narrative=True)

    monkeypatch.setattr(run, "make_edit", choose)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded"
    assert calls["render"] == (10, 45) and calls["wide_render"] == (2, 82)
    assert calls["thumb_at"] == 27 and calls["wide_thumb"][0] == 27
    outputs = json.loads((raw.parent / "meta.json").read_text())["outputs"]
    assert outputs["short"]["context"] == answer["context"]
    assert outputs["landscape"]["reaction"] == answer["reaction"]


@pytest.mark.parametrize("privacy,queued", [("public", True), ("private", False)])
def test_public_short_is_queued_for_instagram_after_render(clip, privacy, queued):
    from dataclasses import replace
    from autoclipper.store import get_output

    s, db, cid, raw, calls = clip
    process_clip(db, replace(s, instagram_account_id="42", privacy=privacy), _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded"
    assert bool(get_output(db, cid, "instagram_reel")) == queued


# --- presentation variants: dark-panel captions with a plain title, or stroked captions with an emoji title

EMOJI_TITLE = GOOD["title"] + " 😭💀"            # the clip is "funny" and the editor gave no emojis


def test_box_variant_renders_box_captions_and_a_plain_title(clip):
    s, db, cid, raw, calls = clip                # nothing posted yet: the first variant, box, is picked
    process_clip(db, s, _row(db, cid))
    assert calls["overlay_style"] == "box" and calls["upload"][0] == GOOD["title"]
    assert _row(db, cid)["variant"] == "box" and _row(db, cid)["title"] == GOOD["title"]


def test_emoji_variant_titles_every_post(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: "emoji")
    process_clip(db, s, _row(db, cid))
    assert calls["overlay_style"] == "stroke" and calls["upload"][0] == EMOJI_TITLE
    assert _row(db, cid)["variant"] == "emoji" and _row(db, cid)["title"] == EMOJI_TITLE


def test_the_variant_is_picked_from_the_configured_list(clip, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = clip
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: calls.setdefault("variants", v)[0])
    process_clip(db, replace(s, caption_variants=("emoji",)), _row(db, cid))
    assert calls["variants"] == ("emoji",) and calls["upload"][0] == EMOJI_TITLE


def test_emoji_variant_reaches_the_companion_instagram_and_meta_json_but_not_the_hook(paired, monkeypatch):
    from dataclasses import replace
    from autoclipper.store import get_output
    s, db, cid, raw, calls = paired
    s = replace(s, instagram_account_id="42")
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: "emoji")
    process_clip(db, s, _row(db, cid))
    assert calls["overlay_styles"] == ["stroke", "stroke"]                    # the Short and the 16:9 overlay
    assert [hook for hook, _ in calls["overlays"]] == [GOOD["hook"], ""] and calls["wide_thumb"][1] == GOOD["hook"]
    assert calls["uploads"] == [("short.mp4", True, "public", EMOJI_TITLE)]
    assert json.loads(_out(db, cid)["meta"])["title"] == EMOJI_TITLE and json.loads(_out(db, cid)["meta"])["hook"] == GOOD["hook"]
    caption = json.loads(get_output(db, cid, "instagram_reel")["meta"])["caption"]
    assert caption.splitlines()[0] == EMOJI_TITLE
    assert run.companion_once(db, s, now=LATER) == cid
    assert calls["uploads"][-1] == ("landscape.mp4", False, "public", EMOJI_TITLE)
    meta = json.loads((raw.parent / "meta.json").read_text())
    assert meta["variant"] == "emoji"
    assert meta["outputs"]["short"]["title"] == EMOJI_TITLE and meta["outputs"]["landscape"]["title"] == EMOJI_TITLE


def test_box_variant_styles_the_companion_and_keeps_every_title_plain(paired):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    assert calls["overlay_styles"] == ["box", "box"]
    assert run.companion_once(db, s, now=LATER) == cid
    assert [u[3] for u in calls["uploads"]] == [GOOD["title"], GOOD["title"]]
    meta = json.loads((raw.parent / "meta.json").read_text())
    assert meta["variant"] == "box" and meta["outputs"]["landscape"]["title"] == GOOD["title"]


def test_uploads_off_records_the_variant_and_its_title(paired, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = paired
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: "emoji")
    process_clip(db, replace(s, upload=False), _row(db, cid))
    row = _row(db, cid)
    assert row["state"] == "saved" and row["variant"] == "emoji" and row["title"] == EMOJI_TITLE
    meta = json.loads((raw.parent / "meta.json").read_text())
    assert meta["variant"] == "emoji" and meta["title"] == EMOJI_TITLE and meta["hook"] == GOOD["hook"]
    assert meta["outputs"]["short"]["title"] == EMOJI_TITLE and meta["outputs"]["landscape"]["title"] == EMOJI_TITLE


def test_a_clip_put_back_after_its_upload_started_keeps_its_variant(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    upload = run.upload_short
    def limit(*a): raise UploadLimit()
    monkeypatch.setattr(run, "upload_short", limit)
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: "emoji")
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and _row(db, cid)["variant"] == "emoji"
    pause(db, "primary", "")
    monkeypatch.setattr(run, "upload_short", upload)
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: "box")      # the split now favours box
    shutil.rmtree(raw.parent / "overlay")                              # the fake overlay builder wants a new folder
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["variant"] == "emoji" and calls["upload"][0] == EMOJI_TITLE
    assert calls["overlay_style"] == "stroke"


# --- per-variant channel routing: box on Channel A (backup, plus Instagram), emoji on Channel B (primary)

ROUTED = {"variant_profiles": {"box": "backup", "emoji": "primary"}, "instagram_profiles": ("backup",),
          "instagram_account_id": "42"}


def _another(db, tmp_path, name="c2"):
    """A second cut clip with its own folder, like the clip fixture's."""
    raw = tmp_path / "clips" / name / "raw.ts"; raw.parent.mkdir(parents=True); raw.write_bytes(b"raw")
    shutil.copy(tmp_path / "clips" / "c1" / "context.json", raw.parent / "context.json")
    return add_clip(db, "twitch:kaicenat", 1000.0, 1010.0, 9.0, "funny", ["LUL"], str(raw), 970.0)


def _posted(db, profile, variant, at, video_id="old"):
    cid = add_clip(db, "twitch:a", 900.0, 920.0, 5.0, "funny", [], "/x/r.ts", 870.0)
    set_state(db, cid, "uploaded", video_id=video_id, uploaded_at=at, youtube_profile=profile, variant=variant)
    return cid


def test_each_variant_posts_on_its_own_channel_and_only_box_goes_to_instagram(clip, tmp_path, monkeypatch):
    from dataclasses import replace
    from autoclipper.store import get_output
    s, db, cid, raw, calls = clip
    s = replace(s, **ROUTED)
    opened = []
    monkeypatch.setattr(run, "client", lambda profile: opened.append(profile) or (f"yt-{profile}", f"UC-{profile}"))
    uploaded = []
    monkeypatch.setattr(run, "upload_short", lambda yt, video, meta, credit, privacy: uploaded.append(yt) or ("v-" + yt, privacy))
    process_clip(db, s, _row(db, cid))                                   # nothing posted yet: box
    assert _row(db, cid)["variant"] == "box" and _row(db, cid)["youtube_profile"] == "backup"
    assert opened == ["backup"] and uploaded == ["yt-backup"] and _row(db, cid)["video_id"] == "v-yt-backup"
    reel = get_output(db, cid, "instagram_reel")
    assert reel["youtube_profile"] == "backup" and json.loads(reel["meta"])["account_id"] == "42"

    second = _another(db, tmp_path)                                      # box has one post: emoji, on primary
    process_clip(db, s, _row(db, second))
    assert _row(db, second)["variant"] == "emoji" and _row(db, second)["youtube_profile"] == "primary"
    assert opened == ["backup", "primary"] and uploaded == ["yt-backup", "yt-primary"]
    assert get_output(db, second, "instagram_reel") is None


@pytest.mark.parametrize("variant,profile,chrome", [("box", "backup", "chrome-profiles/backup"),
                                                    ("emoji", "primary", "chrome-profile")])
def test_the_studio_fallback_opens_and_verifies_the_clips_own_channel(paired, monkeypatch, variant, profile, chrome):
    from dataclasses import replace
    s, db, cid, raw, calls = paired
    s = replace(s, **ROUTED)
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: variant)
    def quota(*a, **k): raise QuotaExhausted()
    monkeypatch.setattr(run, "upload_short", quota)
    monkeypatch.setattr(run, "set_thumbnail", lambda yt, vid, jpg: False)
    opened, uploads = [], []
    @contextlib.contextmanager
    def ctx(chrome_profile):
        opened.append(chrome_profile); yield chrome_profile
    monkeypatch.setattr(run.studio, "context", ctx)
    def studio_upload(c, video, meta, credit, privacy, *, channel_id=None, short=True):
        uploads.append((c, video.name, channel_id)); return f"s-{video.name}"
    monkeypatch.setattr(run.studio, "studio_upload", studio_upload)
    process_clip(db, s, _row(db, cid))
    assert run.companion_once(db, s, now=LATER) == cid
    home = s.data_dir / chrome
    assert uploads == [(home, "short.mp4", f"UC-{profile}"), (home, "landscape.mp4", f"UC-{profile}")]
    assert set(opened) == {home} and _row(db, cid)["youtube_profile"] == profile


def test_cap_and_gap_are_per_channel(tmp_path):
    db = open_db(tmp_path / "a.db")
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, max_uploads_per_day=1, min_gap_minutes=5,
                 variant_profiles={"box": "backup", "emoji": "primary"})
    _posted(db, "primary", "emoji", 1000.0)                              # primary is at its cap of one a day
    assert not run.post_ok(db, s, 2000.0, "primary") and run.post_ok(db, s, 2000.0, "backup")
    assert run.post_ok(db, s, 2000.0)                                    # a slot can still post on backup
    assert run.postable(db, s, 2000.0) == ("box",)
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, min_gap_minutes=5,
                 variant_profiles={"box": "backup", "emoji": "primary"})
    _posted(db, "backup", "box", 1900.0)                                 # backup posted 100 s ago: inside its gap
    assert run.post_ok(db, s, 2000.0, "primary") and not run.post_ok(db, s, 2000.0, "backup")
    assert run.postable(db, s, 2000.0) == ("emoji",)
    assert run.post_ok(db, s, 2200.0, "backup") and run.postable(db, s, 2200.0) == ("box", "emoji")


def test_a_slot_posts_on_the_free_channel_when_the_favoured_variants_channel_is_at_its_cap(clip, tmp_path,
                                                                                         monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = clip
    s = replace(s, **ROUTED, max_uploads_per_day=1)
    monkeypatch.setattr(run, "choose_clips", ORIGINAL_CHOOSE)          # the real ranking (no Jev key: detector score)
    monkeypatch.setattr(run.time, "time", lambda: 200000.0)
    db.execute('update clips set onset = 199900, "end" = 199910, raw_t0 = 199870 where id = ?', (cid,))
    _posted(db, "backup", "box", 1.0); _posted(db, "backup", "box", 2.0)   # long ago: box is ahead in the split
    _posted(db, "primary", "emoji", 199000.0)                               # but emoji's channel is at its cap
    opened = []
    monkeypatch.setattr(run, "client", lambda profile: opened.append(profile) or ("yt", f"UC-{profile}"))
    assert asyncio.run(slot_once(s, db, now=lambda: 200000.0)) == cid
    assert _row(db, cid)["state"] == "uploaded" and _row(db, cid)["variant"] == "box"
    assert _row(db, cid)["youtube_profile"] == "backup" and opened == ["backup"]
    second = _another(db, tmp_path)                                         # now both channels are at their cap
    db.execute('update clips set onset = 201900, "end" = 201910, raw_t0 = 201870 where id = ?', (second,))
    monkeypatch.setattr(run.time, "time", lambda: 202000.0)
    assert asyncio.run(slot_once(s, db, now=lambda: 202000.0)) is None
    assert _row(db, second)["state"] == "cut" and opened == ["backup"]


def test_an_upload_limit_on_one_channel_pauses_only_that_channel(clip, tmp_path, monkeypatch):
    from dataclasses import replace
    from autoclipper.store import pick_variant
    s, db, cid, raw, calls = clip
    s = replace(s, **ROUTED)
    _posted(db, "backup", "box", 1.0)                                    # box is ahead: this clip is emoji
    def limit(yt, *a):
        if yt == "yt-primary": raise UploadLimit()
        return "vid-box", "public"
    monkeypatch.setattr(run, "client", lambda profile: (f"yt-{profile}", f"UC-{profile}"))
    monkeypatch.setattr(run, "upload_short", limit)
    monkeypatch.setattr(run.time, "time", lambda: 5000.0)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and _row(db, cid)["variant"] == "emoji"
    assert paused(db, "primary") == "limit:15800" and paused(db, "backup") == ""
    assert run.postable(db, s, 5000.0) == ("box",) and pick_variant(db, run.postable(db, s, 5000.0)) == "box"
    second = _another(db, tmp_path)
    process_clip(db, s, _row(db, second))                                # emoji is behind, but primary is paused
    assert _row(db, second)["variant"] == "box" and _row(db, second)["youtube_profile"] == "backup"
    assert _row(db, second)["state"] == "uploaded"
    process_clip(db, s, _row(db, cid))                                   # the emoji clip keeps waiting for primary
    assert _row(db, cid)["state"] == "cut" and _row(db, cid)["variant"] == "emoji"


def test_no_channel_can_post_puts_the_clip_back_before_any_work(clip, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = clip
    s = replace(s, **ROUTED)
    pause(db, "primary", "auth"); pause(db, "backup", "strike:x")
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and "transcribed" not in calls


def test_an_auth_failure_pauses_only_its_channel(clip, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = clip
    s = replace(s, **ROUTED)
    def broken(profile):
        if profile == "backup": raise run.SignInError("token revoked")
        return "yt", f"UC-{profile}"
    monkeypatch.setattr(run, "client", broken)
    process_clip(db, s, _row(db, cid))
    assert paused(db, "backup") == "auth" and paused(db, "primary") == ""
    assert "--profile backup" in calls["notes"][0] and _row(db, cid)["youtube_profile"] == "primary"


def test_the_companion_posts_on_its_clips_channel(paired, tmp_path, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = paired
    s = replace(s, **ROUTED, max_uploads_per_day=2)
    opened = []
    monkeypatch.setattr(run, "client", lambda profile: opened.append(profile) or ("yt", f"UC-{profile}"))
    process_clip(db, s, _row(db, cid))                                   # box, on backup
    assert _out(db, cid)["youtube_profile"] == "backup"
    _posted(db, "primary", "emoji", run.time.time(), video_id="p1")      # primary is busy and later at its cap
    _posted(db, "primary", "emoji", run.time.time(), video_id="p2")
    assert run.companion_once(db, s, now=LATER) == cid
    assert opened == ["backup", "backup"] and calls["uploads"][-1][0] == "landscape.mp4"
    assert _out(db, cid)["state"] == "uploaded"


def test_the_companion_waits_for_its_own_channels_cap(paired, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = paired
    s = replace(s, **ROUTED, max_uploads_per_day=1)
    process_clip(db, s, _row(db, cid))                                   # box Short: backup is at its cap
    assert run.companion_once(db, s, now=LATER) is None and _out(db, cid)["state"] == "pending"


def test_the_guard_checks_each_channel_with_its_own_client(tmp_path):
    db = open_db(tmp_path / "a.db")
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, variant_profiles={"box": "backup", "emoji": "primary"})
    _posted(db, "backup", "box", 2.0, video_id="b1"); _posted(db, "primary", "emoji", 3.0, video_id="p1")
    pause(db, "backup", "limit:1")                                       # expired: lifted
    checked, notes = [], []
    def check(ids, profile):
        checked.append((ids, profile)); return ["p1"] if profile == "primary" else []
    asyncio.run(guard_once(s, db, check=check, notify=notes.append))
    assert checked == [(["b1"], "backup"), (["p1"], "primary")]
    assert paused(db, "primary") == "strike:p1" and paused(db, "backup") == ""
    assert len(notes) == 1 and "--profile primary" in notes[0]
    checked.clear()
    asyncio.run(guard_once(s, db, check=check, notify=notes.append))   # a struck channel is not checked again
    assert checked == [(["b1"], "backup")]


def test_instagram_advances_each_instagram_channel(tmp_path, monkeypatch):
    from dataclasses import replace
    db = open_db(tmp_path / "a.db")
    s = replace(Settings(streams=(), data_dir=tmp_path, volume=tmp_path),
                **{**ROUTED, "instagram_profiles": ("backup", "primary")})
    seen = []
    monkeypatch.setattr(run.instagram, "advance", lambda db, profile, account, stop, logger=None: seen.append((profile, account)))
    async def stop(n): raise asyncio.CancelledError
    monkeypatch.setattr(run.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run._instagram_loop(s, db))
    assert seen == [("backup", "42"), ("primary", "42")]


def test_a_clip_waiting_for_its_paused_channel_does_not_take_the_slot_or_fast_lane(tmp_path, monkeypatch):
    db, ids = _db(tmp_path, [9.0, 5.0]); done = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, variant_profiles={"box": "backup", "emoji": "primary"})
    set_state(db, ids[0], "cut", variant="emoji")                       # put back after its upload on primary started
    pause(db, "primary", "limit:99999999999")
    process = lambda db, s, row: done.append(row["id"])
    assert asyncio.run(slot_once(s, db, process=process, now=lambda: 1000.0)) == ids[1] and done == [ids[1]]
    monkeypatch.setattr(run, "SECRETS", {"jev": "key", "twitch": None, "ok": True})
    monkeypatch.setattr(run, "_prepared", lambda db, s, row: {})
    monkeypatch.setattr(run, "score_clip", lambda db, s, row, a, key: 1.0)
    done.clear()
    assert run.prep_once(db, s, process=process, now=lambda: 1000.0) == ids[1] and done == [ids[1]]


# --- review round: routing fixes

def test_a_non_public_retry_drops_the_reel_queued_by_the_public_attempt(clip, monkeypatch):
    from dataclasses import replace
    from autoclipper.store import get_output
    s, db, cid, raw, calls = clip
    s = replace(s, instagram_account_id="42")
    upload = run.upload_short
    def limit(*a): raise UploadLimit()
    monkeypatch.setattr(run, "upload_short", limit)
    process_clip(db, s, _row(db, cid))                                   # public: the Reel is queued, then a limit
    assert get_output(db, cid, "instagram_reel")["state"] == "pending" and _row(db, cid)["state"] == "cut"
    pause(db, "primary", "")
    monkeypatch.setattr(run, "upload_short", upload)
    shutil.rmtree(raw.parent / "overlay")
    process_clip(db, replace(s, privacy="unlisted"), _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded" and _row(db, cid)["privacy"] == "unlisted"
    assert get_output(db, cid, "instagram_reel")["state"] == "dropped"


def test_a_public_short_records_its_privacy(clip):
    s, db, cid, raw, calls = clip
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["privacy"] == "public"


def test_the_fast_lane_reads_the_clip_again_under_heavy(tmp_path, monkeypatch):
    db, ids = _db(tmp_path, [9.0])
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, variant_profiles={"box": "backup", "emoji": "primary"})
    stale = run.cut_rows(db, 1000.0, 3600)                               # read before the clip kept its variant
    set_state(db, ids[0], "cut", variant="emoji")
    pause(db, "primary", "limit:99999999999")
    monkeypatch.setattr(run, "cut_rows", lambda *a: stale)
    monkeypatch.setattr(run, "SECRETS", {"jev": "key", "twitch": None, "ok": True})
    monkeypatch.setattr(run, "_prepared", lambda db, s, row: {})
    monkeypatch.setattr(run, "score_clip", lambda db, s, row, a, key: 1.0)
    done = []
    assert run.prep_once(db, s, process=lambda db, s, row: done.append(row["variant"]), now=lambda: 1000.0) is None
    assert done == []


def test_without_routing_the_cap_and_gap_count_every_channel_as_before(tmp_path):
    db = open_db(tmp_path / "a.db")
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, max_uploads_per_day=1, min_gap_minutes=5)
    _posted(db, "backup", "box", 1000.0)                                 # an old row from another profile
    assert not run.post_ok(db, s, 2000.0, "primary") and not run.post_ok(db, s, 2000.0)
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, max_uploads_per_day=5, min_gap_minutes=60)
    assert not run.post_ok(db, s, 2000.0, "primary")                    # inside the gap of that upload


def test_routed_variant_choice_balances_per_destination(clip, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = clip
    s = replace(s, **ROUTED)
    for _ in range(3):
        _posted(db, "primary", "box", 1.0)                               # from before routing: box ran on primary
    _posted(db, "primary", "emoji", 1.0)
    monkeypatch.setattr(run, "client", lambda profile: ("yt", f"UC-{profile}"))
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["variant"] == "box" and _row(db, cid)["youtube_profile"] == "backup"


def test_main_moves_the_old_global_pause_into_every_profile(tmp_path, monkeypatch):
    (tmp_path / "s.toml").write_text(f'data_dir="{tmp_path}"\nvolume="{tmp_path}"\n'
                                     'variant_profiles = { box = "backup", emoji = "primary" }\n'
                                     '[[stream]]\nplatform="twitch"\nchannel="a"\n')
    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(run, "check_channels", lambda db, s: None, raising=False)
    db = open_db(tmp_path / "autoclipper.db"); flag(db, "uploads_paused", "strike:v1")
    async def crash(*a, **k): raise RuntimeError("stop here")
    monkeypatch.setattr(run, "stream_task", crash)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    assert paused(db, "backup") == paused(db, "primary") == "strike:v1" and flag(db, "uploads_paused") == ""


def test_a_quota_error_while_signing_in_goes_to_studio_and_never_pauses(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def quota(profile): raise QuotaExhausted()
    monkeypatch.setattr(run, "client", quota)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded" and _row(db, cid)["video_id"] == "sid1"
    assert calls["studio_channel"] == "UC-primary" and paused(db, "primary") == "" and "notes" not in calls


def test_a_quota_error_without_a_verified_channel_waits_without_pausing(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def quota(profile): raise QuotaExhausted()
    monkeypatch.setattr(run, "client", quota)
    monkeypatch.setattr(run, "verified_channel", lambda profile: None)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and paused(db, "primary") == "" and "transcribed" not in calls


def test_the_companion_loop_moves_on_when_one_channel_cannot_sign_in(paired, tmp_path, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = paired
    s = replace(s, **ROUTED, caption_variants=("emoji", "box"))         # primary is tried first
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: "emoji")
    process_clip(db, s, _row(db, cid))                                   # an emoji companion waits on primary
    second = _another(db, tmp_path)
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: "box")
    process_clip(db, s, _row(db, second))                               # and a box companion on backup
    def only_backup(profile):
        if profile == "primary": raise run.SignInError("token revoked")
        return "yt", f"UC-{profile}"
    monkeypatch.setattr(run, "client", only_backup)
    assert run.companion_once(db, s, now=LATER) == second              # primary fails, backup posts in this pass
    assert paused(db, "primary") == "auth" and _out(db, cid)["state"] == "pending"


def test_startup_signs_in_each_channel_and_refuses_two_profiles_on_one_channel(tmp_path, monkeypatch):
    db = open_db(tmp_path / "a.db"); notes = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, variant_profiles={"box": "backup", "emoji": "primary"})
    monkeypatch.setattr(run, "notify", notes.append)
    monkeypatch.setattr(run, "client", lambda profile: ("yt", "UC-same"))
    run.check_channels(db, s)
    assert paused(db, "backup").startswith("duplicate:") and paused(db, "primary").startswith("duplicate:")
    assert len(notes) == 1 and "UC-same" in notes[0]
    monkeypatch.setattr(run, "client", lambda profile: ("yt", f"UC-{profile}"))
    run.check_channels(db, s)                                           # still paused until someone resumes
    assert paused(db, "backup").startswith("duplicate:")


def test_startup_pauses_a_channel_that_cannot_sign_in_before_any_slot(tmp_path, monkeypatch):
    db, ids = _db(tmp_path, [5.0]); done = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, variant_profiles={"box": "backup", "emoji": "primary"})
    monkeypatch.setattr(run, "notify", lambda text: None)
    def client(profile):
        if profile == "backup": raise run.SignInError("token revoked")
        return "yt", f"UC-{profile}"
    monkeypatch.setattr(run, "client", client)
    monkeypatch.setattr(run, "verified_channel", lambda profile: f"UC-{profile}")
    run.check_channels(db, s)
    assert paused(db, "backup") == "auth" and paused(db, "primary") == ""
    assert run.postable(db, s, 1000.0) == ("emoji",)


def test_startup_checks_nothing_without_routing(tmp_path, monkeypatch):
    def no_youtube(profile): raise AssertionError("no startup sign-in without routing")
    monkeypatch.setattr(run, "client", no_youtube)
    run.check_channels(open_db(tmp_path / "a.db"), Settings(streams=(), data_dir=tmp_path, volume=tmp_path))


def test_the_lock_is_held_until_a_running_posting_worker_finishes(tmp_path, monkeypatch):
    import threading
    import time as real_time
    _toml(tmp_path)
    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    started, order = threading.Event(), []
    def slow_companion(db, s, *a, **k):
        started.set(); real_time.sleep(0.3); order.append("worker done")
    monkeypatch.setattr(run, "companion_once", slow_companion)
    async def crash(*a, **k):
        await asyncio.to_thread(started.wait, 5); raise RuntimeError("stop here")
    monkeypatch.setattr(run, "stream_task", crash)
    class Lock:
        def __init__(self, f): self.f = f
        def fileno(self): return self.f.fileno()
        def close(self): order.append("lock released"); self.f.close()
    monkeypatch.setattr(run, "open", lambda path, mode="r": Lock(open(path, mode)), raising=False)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    assert order == ["worker done", "lock released"] and run.STOP.is_set()


# --- live pilot: game names of fixed streams, and Reels Instagram can process

def test_a_fixed_streams_category_comes_from_its_platform(monkeypatch):
    class Helix:
        def category(self, login): return {"tfue": "ROB RUN"}.get(login)
    monkeypatch.setattr(run, "_HELIX", [Helix()])
    monkeypatch.setattr(run.disc, "kick_category", lambda slug: "Grand Theft Auto VI (GTA)")
    monkeypatch.setattr(run.disc, "live_category", lambda url: "Minecraft")
    assert ORIGINAL_GAME(StreamCfg("twitch", "tfue")) == "ROB RUN"
    assert ORIGINAL_GAME(StreamCfg("kick", "adinross")) == "Grand Theft Auto VI (GTA)"
    assert ORIGINAL_GAME(StreamCfg("youtube", "@IShowSpeed")) == "Minecraft"
    monkeypatch.setattr(run, "_HELIX", [None])                           # no Twitch credentials: streamlink's metadata
    assert ORIGINAL_GAME(StreamCfg("twitch", "tfue")) == "Minecraft"


def _games(monkeypatch, answers, *, passes, probe_s=0.0):
    """Run _game_loop for one fixed stream over `passes` refreshes on a fake clock; each lookup takes probe_s.
    Returns (game after each pass, each sleep)."""
    answers, clock = iter(answers), {"t": 0.0}
    def fixed_game(cfg):
        clock["t"] += probe_s
        got = next(answers)
        if isinstance(got, Exception): raise got
        return got
    monkeypatch.setattr(run, "fixed_game", fixed_game)
    cfg = StreamCfg("twitch", "tfue"); states = {cfg.key: {}}; seen, waits = [], []
    async def sleep(n):
        waits.append(n); seen.append(states[cfg.key].get("game")); clock["t"] += n
        if len(waits) == passes: raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run._game_loop([cfg], states, sleep=sleep, clock=lambda: clock["t"]))
    return seen, waits


def test_a_fixed_streams_game_follows_each_lookup_and_survives_a_short_failure(monkeypatch):
    seen, waits = _games(monkeypatch, ["ROB RUN", RuntimeError("helix down"), None, "Fortnite"], passes=4)
    assert seen == ["ROB RUN", "ROB RUN", "", "Fortnite"] and waits == [300, 300, 300, 300]   # no game: cleared


def test_the_refresh_keeps_five_minute_deadlines_however_long_the_lookups_take(monkeypatch):
    seen, waits = _games(monkeypatch, ["ROB RUN", "ROB RUN", "ROB RUN"], passes=3, probe_s=100.0)
    assert waits == [200, 200, 200]


def test_a_game_name_expires_after_fifteen_minutes_of_failed_lookups(monkeypatch):
    down = RuntimeError("helix down")
    seen, waits = _games(monkeypatch, ["ROB RUN", down, down, down, down], passes=5)
    assert seen == ["ROB RUN", "ROB RUN", "ROB RUN", "", ""]           # 900 s after the last good lookup


def test_youtubes_broad_categories_are_never_a_game(monkeypatch):
    monkeypatch.setattr(run.disc, "live_category", lambda url: "Gaming")
    assert ORIGINAL_GAME(StreamCfg("youtube", "@IShowSpeed")) is None
    monkeypatch.setattr(run.disc, "live_category", lambda url: "People & Blogs")
    assert ORIGINAL_GAME(StreamCfg("youtube", "@IShowSpeed")) is None
    monkeypatch.setattr(run.disc, "live_category", lambda url: "Grand Theft Auto VI")
    assert ORIGINAL_GAME(StreamCfg("youtube", "@IShowSpeed")) == "Grand Theft Auto VI"
    class Helix:
        def category(self, login): return "Just Chatting"                # a real Twitch category: kept
    monkeypatch.setattr(run, "_HELIX", [Helix()])
    assert ORIGINAL_GAME(StreamCfg("twitch", "tfue")) == "Just Chatting"


def test_a_pinned_youtube_stream_never_gets_a_broad_category_as_its_game(tmp_path):
    pin = StreamCfg("youtube", "@IShowSpeed")
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, discover=_d(pin=(pin,), prefer=()))
    started = []
    async def run_stream(cfg, state):
        started.append((cfg.key, state["game"])); await asyncio.Event().wait()
    async def sleep(n):
        await asyncio.sleep(0.01); raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run.discover_loop(s, open_db(tmp_path / "a.db"), lister=lambda d: ({}, {"twitch"}),
                                      allow=lambda listing, d: listing, probe=lambda url: (True, "Gaming"),
                                      run_stream=run_stream, sleep=sleep))
    assert started == [("youtube:@IShowSpeed", "")]


def test_main_gives_each_fixed_stream_a_state_the_game_loop_fills(tmp_path, monkeypatch):
    _toml(tmp_path)
    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    given = {}
    async def stream(cfg, s, db, *, state=None, **k):
        given["stream"] = state; raise RuntimeError("stop here")
    async def games(fixed, states, **k):
        given["games"] = states
    monkeypatch.setattr(run, "stream_task", stream)
    monkeypatch.setattr(run, "_game_loop", games)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    assert given["stream"] is given["games"]["twitch:a"]


def test_a_long_short_queues_a_reel_cut_of_at_most_50_seconds(clip, monkeypatch):
    from dataclasses import replace
    from autoclipper.store import get_output
    s, db, cid, raw, calls = clip
    s = replace(s, instagram_account_id="42")
    monkeypatch.setattr(run, "_duration", lambda media, ff: 200.0)
    def edit(db, day, streamer, game, platform, mood, words, chat, onset_s, raw_s, cwd=None):
        out = {**GOOD, "keep": True, "what_happened": "Kai reads a wild pizza order out loud.", "start": 10.0, "end": 182.0}
        fake = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")
        return make_edit(db, day, streamer, game, platform, mood, words, chat, onset_s, raw_s, run=fake, cwd=cwd)
    monkeypatch.setattr(run, "make_edit", edit)
    renders, styles = [], []
    def render(raw_, a, b, overlay, out, ff):
        renders.append((out.name, a, b)); out.write_bytes(b"mp4"); return out
    monkeypatch.setattr(run, "render_short", render)
    def reel_encode(raw_, a, b, overlay, out, ff):
        renders.append((out.name + " at the Reel bitrate", a, b)); out.write_bytes(b"mp4"); return out
    monkeypatch.setattr(run, "render_reel", reel_encode)
    def overlay(w, a, b, hook, d, layout=None, style="stroke"):
        styles.append((d.name, style)); d.mkdir(); (d / "subs.ffconcat").write_text("x"); return d / "subs.ffconcat"
    monkeypatch.setattr(run, "build_overlay", overlay)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded"
    assert [r for r in renders if r[0] == "reel.mp4"] == []                  # never at the Short's bitrate
    (name, a, b), = [r for r in renders if r[0] == "reel.mp4 at the Reel bitrate"]
    assert b - a <= 50 and a <= 30.0 - 3 and (a, b) == (25.0, 75.0)          # the onset at 30 s, from 5 s before
    assert ("overlay_reel", "box") in styles and dict(styles)["overlay"] == "box"
    reel = get_output(db, cid, "instagram_reel")
    assert reel["video_path"].endswith("reel.mp4") and json.loads(reel["meta"])["cover_ms"] == 6000   # thumb at 31 s
    assert json.loads((raw.parent / "meta.json").read_text())["outputs"]["instagram_reel"]["window"] == [25.0, 75.0]
    assert (raw.parent / "reel.mp4").exists() and not (raw.parent / "overlay_reel").exists()


def test_a_short_of_50_seconds_or_less_is_its_own_reel(clip):
    from dataclasses import replace
    from autoclipper.store import get_output
    s, db, cid, raw, calls = clip
    process_clip(db, replace(s, instagram_account_id="42"), _row(db, cid))    # the fixture's Short is 22 s
    assert get_output(db, cid, "instagram_reel")["video_path"].endswith("short.mp4")
    assert json.loads((raw.parent / "meta.json").read_text())["outputs"]["instagram_reel"]["window"] == [22.0, 44.0]


# --- second review round

def test_the_guard_never_lifts_a_strike_that_replaced_the_limit_it_read(tmp_path, monkeypatch):
    db = open_db(tmp_path / "a.db"); pause(db, "primary", "strike:v")
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    real, reads = run.paused, []
    def stale(db_, profile):                   # the first read still saw the expired limit; a strike landed since
        reads.append(profile)
        return "limit:1" if len(reads) == 1 else real(db_, profile)
    monkeypatch.setattr(run, "paused", stale)
    asyncio.run(guard_once(s, db, check=lambda ids, profile: [], notify=print))
    assert real(db, "primary") == "strike:v"


def test_a_failed_sign_in_keeps_a_strike_and_does_not_notify(tmp_path, monkeypatch):
    db = open_db(tmp_path / "a.db"); notes = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, variant_profiles={"box": "backup", "emoji": "primary"})
    pause(db, "backup", "strike:v")
    def broken(profile): raise run.SignInError("token revoked")
    monkeypatch.setattr(run, "client", broken)
    monkeypatch.setattr(run, "notify", notes.append)
    run.check_channels(db, s)
    assert paused(db, "backup") == "strike:v" and paused(db, "primary") == "auth" and len(notes) == 1


def _long_short(monkeypatch, on_reel=None):
    """The clip fixture's editor returns a 172 s Short, so a separate Reel is rendered; on_reel runs during it."""
    monkeypatch.setattr(run, "_duration", lambda media, ff: 200.0)
    def edit(db, day, streamer, game, platform, mood, words, chat, onset_s, raw_s, cwd=None):
        out = {**GOOD, "keep": True, "what_happened": "Kai reads a wild pizza order out loud.", "start": 10.0, "end": 182.0}
        fake = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")
        return make_edit(db, day, streamer, game, platform, mood, words, chat, onset_s, raw_s, run=fake, cwd=cwd)
    monkeypatch.setattr(run, "make_edit", edit)
    def render(raw_, a, b, overlay, out, ff):
        if out.name == "reel.mp4" and on_reel:
            return on_reel(out)
        out.write_bytes(b"mp4"); return out
    monkeypatch.setattr(run, "render_short", render)
    monkeypatch.setattr(run, "render_reel", render)
    def overlay(w, a, b, hook, d, layout=None, style="stroke"):
        d.mkdir(exist_ok=True); (d / "subs.ffconcat").write_text("x"); return d / "subs.ffconcat"
    monkeypatch.setattr(run, "build_overlay", overlay)


@pytest.mark.parametrize("during", ["strike", "stop"])
def test_a_pause_or_stop_during_the_reel_render_holds_the_upload(clip, monkeypatch, during):
    from dataclasses import replace
    s, db, cid, raw, calls = clip
    def reel(out):
        pause(db, "primary", "strike:x") if during == "strike" else run.STOP.set()
        out.write_bytes(b"reel"); return out
    _long_short(monkeypatch, reel)
    process_clip(db, replace(s, instagram_account_id="42"), _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and "upload" not in calls


def test_a_stop_while_the_api_upload_fails_never_starts_the_studio_upload(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def quota(*a):
        run.STOP.set(); raise QuotaExhausted()
    monkeypatch.setattr(run, "upload_short", quota)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "cut" and "studio_uploads" not in calls


def test_a_stop_while_the_companions_api_upload_fails_never_starts_its_studio_upload(paired, monkeypatch):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    def quota(*a, **k):
        run.STOP.set(); raise QuotaExhausted()
    monkeypatch.setattr(run, "upload_short", quota)
    assert run.companion_once(db, s, now=LATER) is None
    assert _out(db, cid)["state"] == "pending" and "studio_uploads" not in calls


def test_the_privacy_youtube_reports_decides_the_reel(clip, monkeypatch):
    from dataclasses import replace
    from autoclipper.store import get_output
    s, db, cid, raw, calls = clip
    monkeypatch.setattr(run, "upload_short", lambda yt, video, meta, credit, privacy: ("vid1", "private"))
    process_clip(db, replace(s, instagram_account_id="42"), _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded" and _row(db, cid)["privacy"] == "private"
    assert get_output(db, cid, "instagram_reel")["state"] == "dropped"


def test_a_studio_upload_records_the_privacy_it_set(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    def quota(*a): raise QuotaExhausted()
    monkeypatch.setattr(run, "upload_short", quota)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["video_id"] == "sid1" and _row(db, cid)["privacy"] == "public"


def test_the_companion_records_the_privacy_youtube_reports(paired, monkeypatch):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    monkeypatch.setattr(run, "upload_short", lambda yt, video, meta, credit, privacy, *, short=True: ("w1", "unlisted"))
    assert run.companion_once(db, s, now=LATER) == cid
    assert json.loads(_out(db, cid)["meta"])["uploaded_privacy"] == "unlisted"


def test_a_retry_drops_the_old_reel_and_requeues_only_a_valid_new_one(clip, monkeypatch):
    from dataclasses import replace
    from autoclipper.store import get_output
    s, db, cid, raw, calls = clip
    s = replace(s, instagram_account_id="42")
    upload = run.upload_short
    def limit(*a): raise UploadLimit()
    monkeypatch.setattr(run, "upload_short", limit)
    _long_short(monkeypatch)
    process_clip(db, s, _row(db, cid))                                   # attempt 1: Reel queued, then a limit
    assert get_output(db, cid, "instagram_reel")["state"] == "pending"
    pause(db, "primary", "")
    monkeypatch.setattr(run, "upload_short", upload)
    def broken(out): raise RuntimeError("ffmpeg exploded")
    _long_short(monkeypatch, broken)                                     # attempt 2: the Reel render fails
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded" and get_output(db, cid, "instagram_reel")["state"] == "dropped"


def test_an_empty_reel_file_is_never_queued(clip, monkeypatch):
    from dataclasses import replace
    from autoclipper.store import get_output
    s, db, cid, raw, calls = clip
    def empty(out): out.write_bytes(b""); return out
    _long_short(monkeypatch, empty)
    process_clip(db, replace(s, instagram_account_id="42"), _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded" and get_output(db, cid, "instagram_reel") is None


def test_the_short_uses_the_channel_its_client_was_verified_with(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    current = {"primary": "UC-primary"}
    monkeypatch.setattr(run, "verified_channel", lambda profile: current[profile])
    def quota(*a):
        current["primary"] = "UC-changed"; raise QuotaExhausted()     # a guard sign-in updated the cache meanwhile
    monkeypatch.setattr(run, "upload_short", quota)
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["video_id"] == "sid1" and calls["studio_channel"] == "UC-primary"


def test_the_companion_uses_the_channel_its_client_was_verified_with(paired, monkeypatch):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    current = {"primary": "UC-primary"}
    monkeypatch.setattr(run, "verified_channel", lambda profile: current[profile])
    def quota(*a, **k):
        current["primary"] = "UC-changed"; raise QuotaExhausted()
    monkeypatch.setattr(run, "upload_short", quota)
    seen = []
    monkeypatch.setattr(run.studio, "studio_upload",
                        lambda c, video, meta, credit, privacy, *, channel_id=None, short=True: seen.append(channel_id) or "sw")
    assert run.companion_once(db, s, now=LATER) == cid and seen == ["UC-primary"]


def test_a_guard_sign_in_that_lands_on_another_profiles_channel_pauses_both(tmp_path, monkeypatch):
    db = open_db(tmp_path / "a.db"); notes = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, variant_profiles={"box": "backup", "emoji": "primary"})
    current = {"backup": "UC-a", "primary": "UC-b"}
    monkeypatch.setattr(run, "client", lambda profile: ("yt", current[profile]))
    monkeypatch.setattr(run, "notify", notes.append)
    monkeypatch.setattr(run, "strike_check", lambda yt, ids: [])
    run.check_channels(db, s)
    assert paused(db, "backup") == paused(db, "primary") == ""
    _posted(db, "backup", "box", 2.0, video_id="b1")
    current["backup"] = "UC-b"                                       # backup's sign-in now resolves to Channel B
    asyncio.run(guard_once(s, db, notify=notes.append))
    assert paused(db, "backup").startswith("duplicate:") and paused(db, "primary").startswith("duplicate:")
    assert len(notes) == 1


# --- third review round

from google.auth.exceptions import RefreshError, TransportError as AuthTransportError


@pytest.mark.parametrize("error", [
    RefreshError("('Connection aborted.', RemoteDisconnected('Remote end closed connection'))"),
    RefreshError("server_error: try again", {"error": "server_error"}, retryable=True),
    AuthTransportError("timed out"),
])
def test_a_transient_sign_in_failure_never_pauses(tmp_path, monkeypatch, error):
    db = open_db(tmp_path / "a.db"); notes = []
    def broken(profile): raise error
    monkeypatch.setattr(run, "client", broken)
    monkeypatch.setattr(run, "notify", notes.append)
    yt, why, channel = run._youtube(db, "backup")
    assert yt is None and paused(db, "backup") == "" and notes == []


@pytest.mark.parametrize("error", [
    RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"}),
    RefreshError("deleted_client: The OAuth client was deleted.", {"error": "deleted_client"}),
    RefreshError("invalid_client: Unauthorized", {"error": "invalid_client"}),
])
def test_a_definitive_credential_failure_pauses_the_channel(tmp_path, monkeypatch, error):
    db = open_db(tmp_path / "a.db"); notes = []
    def broken(profile): raise error
    monkeypatch.setattr(run, "client", broken)
    monkeypatch.setattr(run, "notify", notes.append)
    run._youtube(db, "backup")
    assert paused(db, "backup") == "auth" and len(notes) == 1


def test_the_studio_fallback_uses_the_channel_returned_with_the_client_not_the_cache(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    monkeypatch.setattr(run, "client", lambda profile: ("yt", "UC-from-sign-in"))
    monkeypatch.setattr(run, "verified_channel", lambda profile: "UC-from-cache")
    def quota(*a): raise QuotaExhausted()
    monkeypatch.setattr(run, "upload_short", quota)
    process_clip(db, s, _row(db, cid))
    assert calls["studio_channel"] == "UC-from-sign-in"


def test_the_guard_registers_the_channel_before_any_strike_lookup(tmp_path, monkeypatch):
    db = open_db(tmp_path / "a.db"); notes = []
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path, variant_profiles={"box": "backup", "emoji": "primary"})
    _posted(db, "backup", "box", 2.0, video_id="b1")
    run.CHECKED["primary"] = "UC-b"
    monkeypatch.setattr(run, "client", lambda profile: ("yt", "UC-b"))      # backup now signs in to Channel B
    looked = []
    monkeypatch.setattr(run, "strike_check", lambda yt, ids: looked.append(ids) or [])
    asyncio.run(guard_once(s, db, notify=notes.append))
    assert looked == [] and paused(db, "backup").startswith("duplicate:") and paused(db, "primary").startswith("duplicate:")


def test_a_failed_game_probe_is_a_failure_so_the_name_is_kept(monkeypatch):
    def failed(url): raise run.disc.ProbeFailed("streamlink timed out")
    monkeypatch.setattr(run.disc, "live_category", failed)
    with pytest.raises(run.disc.ProbeFailed):
        ORIGINAL_GAME(StreamCfg("youtube", "@IShowSpeed"))
    monkeypatch.setattr(run, "_HELIX", [None])                           # Twitch without Helix: the same probe
    with pytest.raises(run.disc.ProbeFailed):
        ORIGINAL_GAME(StreamCfg("twitch", "tfue"))
    monkeypatch.setattr(run.disc, "live_category", lambda url: None)    # it answered: no game
    assert ORIGINAL_GAME(StreamCfg("youtube", "@IShowSpeed")) is None


def test_main_moves_an_old_upload_limit_only_to_the_channel_that_hit_it(tmp_path, monkeypatch):
    (tmp_path / "s.toml").write_text(f'data_dir="{tmp_path}"\nvolume="{tmp_path}"\n'
                                     'variant_profiles = { box = "backup", emoji = "primary" }\n'
                                     '[[stream]]\nplatform="twitch"\nchannel="a"\n')
    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(run, "check_channels", lambda db, s: None)
    db = open_db(tmp_path / "autoclipper.db")
    _posted(db, "backup", None, 100.0, video_id="b1")                    # the single-profile config posted on backup
    flag(db, "uploads_paused", "limit:99999999999")
    async def crash(*a, **k): raise RuntimeError("stop here")
    monkeypatch.setattr(run, "stream_task", crash)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    assert paused(db, "backup") == "limit:99999999999" and paused(db, "primary") == ""


# --- round 4

@pytest.mark.parametrize("error", [
    "server-not-found", "socket-timeout", "connection-reset", "http-429", "http-503", "unexpected",
])
def test_a_transient_api_error_never_pauses(tmp_path, monkeypatch, error):
    import socket
    import httplib2
    from googleapiclient.errors import HttpError
    def http(status): return HttpError(resp=httplib2.Response({"status": status}), content=b"{}")
    raised = {"server-not-found": httplib2.ServerNotFoundError("Unable to find the server at oauth2.googleapis.com"),
              "socket-timeout": socket.timeout("timed out"), "connection-reset": ConnectionResetError(54, "reset"),
              "http-429": http(429), "http-503": http(503), "unexpected": ValueError("odd answer")}[error]
    db = open_db(tmp_path / "a.db"); notes = []
    def broken(profile): raise raised
    monkeypatch.setattr(run, "client", broken)
    monkeypatch.setattr(run, "notify", notes.append)
    assert run._youtube(db, "backup")[0] is None
    assert paused(db, "backup") == "" and notes == []


@pytest.mark.parametrize("error", ["sign-in", "http-401", "http-403"])
def test_only_an_identified_credential_failure_pauses(tmp_path, monkeypatch, error):
    import httplib2
    from googleapiclient.errors import HttpError
    def http(status): return HttpError(resp=httplib2.Response({"status": status}),
                                       content=b'{"error": {"errors": [{"reason": "authError"}]}}')
    raised = {"sign-in": run.SignInError("YouTube authorization for profile 'backup' resolved to a different channel"),
              "http-401": http(401), "http-403": http(403)}[error]
    db = open_db(tmp_path / "a.db")
    def broken(profile): raise raised
    monkeypatch.setattr(run, "client", broken)
    monkeypatch.setattr(run, "notify", lambda text: None)
    run._youtube(db, "backup")
    assert paused(db, "backup") == "auth"


# --- round 5

def _http(status, reason):
    import httplib2
    from googleapiclient.errors import HttpError
    body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    return HttpError(resp=httplib2.Response({"status": status}), content=body)


@pytest.mark.parametrize("status,reason", [(403, "userRateLimitExceeded"), (403, "rateLimitExceeded"),
                                           (403, "concurrentLimitExceeded"), (403, "somethingNew"),
                                           (403, "forbidden"), (400, "badRequest"), (404, "notFound")])
def test_a_4xx_without_credential_evidence_never_pauses_and_is_logged(tmp_path, monkeypatch, caplog, status, reason):
    db = open_db(tmp_path / "a.db"); notes = []
    def refused(profile): raise _http(status, reason)
    monkeypatch.setattr(run, "client", refused)
    monkeypatch.setattr(run, "notify", notes.append)
    yt, why, _ = run._youtube(db, "backup")
    assert yt is None and paused(db, "backup") == "" and notes == []
    assert reason in caplog.text


@pytest.mark.parametrize("status,reason", [(401, "authError"), (401, "unauthorized"), (401, ""),
                                           (401, "quotaExceeded"), (403, "insufficientPermissions"),
                                           (403, "insufficientScope"), (403, "authError"), (403, "invalidCredentials")])
def test_a_4xx_with_credential_evidence_pauses(tmp_path, monkeypatch, status, reason):
    db = open_db(tmp_path / "a.db")
    def refused(profile): raise _http(status, reason)
    monkeypatch.setattr(run, "client", refused)
    monkeypatch.setattr(run, "notify", lambda text: None)
    run._youtube(db, "backup")
    assert paused(db, "backup") == "auth"


def test_a_quota_403_while_signing_in_takes_the_quota_path(tmp_path, monkeypatch):
    db = open_db(tmp_path / "a.db")
    def refused(profile): raise _http(403, "quotaExceeded")
    monkeypatch.setattr(run, "client", refused)
    monkeypatch.setattr(run, "verified_channel", lambda profile: "UC-backup")
    assert run._youtube(db, "backup") == (None, run.QUOTA, "UC-backup") and paused(db, "backup") == ""



# --- a clip whose channel cannot sign in waits, and the other channel can take it

def test_a_transient_failure_pauses_nothing_and_the_same_clip_posts_on_the_other_channel_next_pass(clip,
                                                                                                    monkeypatch):
    from dataclasses import replace
    from google.auth.exceptions import TransportError
    s, db, cid, raw, calls = clip
    s = replace(s, **ROUTED)
    _posted(db, "backup", "box", 1.0)                                    # box is ahead: emoji (primary) is first choice
    down = {"primary", "backup"}
    def client(profile):
        if profile in down: raise TransportError("no route to host")
        return "yt", f"UC-{profile}"
    monkeypatch.setattr(run, "client", client)
    assert process_clip(db, s, _row(db, cid)) == run.WAITS              # neither channel can sign in now
    assert _row(db, cid)["state"] == "cut" and _row(db, cid)["variant"] is None and "transcribed" not in calls
    assert paused(db, "primary") == paused(db, "backup") == ""
    down = {"primary"}                                                   # next pass: primary still fails
    process_clip(db, s, _row(db, cid))
    assert _row(db, cid)["state"] == "uploaded" and _row(db, cid)["youtube_profile"] == "backup"
    assert _row(db, cid)["variant"] == "box" and paused(db, "primary") == ""


def test_a_credential_failure_on_the_first_choice_still_lets_the_other_channel_post(clip, monkeypatch):
    from dataclasses import replace
    s, db, cid, raw, calls = clip
    s = replace(s, **ROUTED)
    _posted(db, "backup", "box", 1.0)
    def client(profile):
        if profile == "primary": raise run.SignInError("token revoked")
        return "yt", f"UC-{profile}"
    monkeypatch.setattr(run, "client", client)
    process_clip(db, s, _row(db, cid))
    assert paused(db, "primary") == "auth" and _row(db, cid)["youtube_profile"] == "backup"


def test_a_plain_forbidden_does_not_pause_and_a_401_does(tmp_path, monkeypatch):
    db = open_db(tmp_path / "a.db")
    monkeypatch.setattr(run, "notify", lambda text: None)
    def forbidden(profile): raise _http(403, "forbidden")
    monkeypatch.setattr(run, "client", forbidden)
    assert run._youtube(db, "primary")[0] is None and paused(db, "primary") == ""
    def unauthorised(profile): raise _http(401, "")
    monkeypatch.setattr(run, "client", unauthorised)
    run._youtube(db, "primary")
    assert paused(db, "primary") == "auth"


# --- learning loop: the record, the learned values, and a record that fails

import sqlite3
from dataclasses import replace as _replace

from autoclipper import learn
from autoclipper.meta import Edit, template_meta


@pytest.fixture
def ldb(tmp_path, monkeypatch):
    """The learning record, installed as run.LEARN for the test (monkeypatch puts None back)."""
    db = learn.open_learning(tmp_path / "learning.db")
    monkeypatch.setattr(run, "LEARN", db)
    monkeypatch.setattr(run, "STATS_CLIENTS", {})
    monkeypatch.setattr(run, "EXPLORING", set())
    return db


HALF = {"hook": .5, "context": .5, "payoff": .5, "crowd": .5, "appeal": .5, "story": .5, "dead": 0.0}


class BrokenRecord:
    """A learning database that is locked: every statement raises."""
    def execute(self, *a, **k): raise sqlite3.OperationalError("database is locked")


def _learned_state(ldb, **values):
    flag(ldb, "state", json.dumps(values))


def _cands(ldb):
    return {r["clip_id"]: r for r in ldb.execute("select * from candidates")}


def test_scoring_records_the_candidate(candidates, ldb, monkeypatch):
    s, db, ids = candidates
    monkeypatch.setattr(run.jev, "ask", _jev([0.9, 0.3, 0.6]))
    ORIGINAL_CHOOSE(db, s, run.cut_rows(db, 1100.0, 3600))
    rows = _cands(ldb)
    assert sorted(rows) == sorted(ids)
    assert all(r["stream"] == "twitch:a" and r["game"] == "G" and r["mood"] == "funny" for r in rows.values())
    assert json.loads(rows[ids[0]]["scores"])["hook"] == pytest.approx(0.9)
    assert json.loads(rows[ids[1]]["scores"])["hook"] == pytest.approx(0.3)
    assert rows[ids[0]]["det_score"] == 5.0 and rows[ids[0]]["kept"] is None
    assert "crowd" not in json.loads(rows[ids[0]]["scores"])           # no chat: kept out of the fit and the bars


def test_editor_verdict_and_post_are_recorded(clip, ldb, monkeypatch):
    s, db, cid, raw, calls = clip
    learn.add_candidate(ldb, cid, "twitch:kaicenat", "G", "funny", 9.0, HALF, 1000.0)
    monkeypatch.setattr(run, "pick_variant", lambda db, v, *routes: "emoji")
    monkeypatch.setattr(run.time, "time", lambda: 5000.0)
    process_clip(db, s, _row(db, cid))
    assert calls["upload"][0] == EMOJI_TITLE                           # the post carries the emoji title...
    row = _cands(ldb)[cid]
    assert row["kept"] == 1 and row["video_id"] == "vid1" and row["profile"] == "primary"
    assert row["variant"] == "emoji" and row["title"] == GOOD["title"]    # ...the record keeps the plain one
    assert row["what"] == "Kai reads a wild pizza order out loud." and row["context"] == "" and row["reaction"] == ""
    assert row["length_s"] == 22.0 and row["posted_at"] == 5000.0 and row["explore"] == 0


def test_editor_drop_is_recorded(clip, ldb, monkeypatch):
    s, db, cid, raw, calls = clip
    learn.add_candidate(ldb, cid, "twitch:kaicenat", "G", "funny", 9.0, HALF, 1000.0)
    monkeypatch.setattr(run, "make_edit", lambda *a, **k: Edit(False, "Nothing happens.", None, None,
                                                               template_meta("", "", 0), "haiku"))
    process_clip(db, s, _row(db, cid))
    row = _cands(ldb)[cid]
    assert _row(db, cid)["state"] == "dropped" and row["kept"] == 0 and row["video_id"] is None


def test_template_edit_records_no_verdict(clip, ldb, monkeypatch):
    s, db, cid, raw, calls = clip
    learn.add_candidate(ldb, cid, "twitch:kaicenat", "G", "funny", 9.0, HALF, 1000.0)
    run.EXPLORING.add(cid)
    # the editor was never really asked (capped or failed): make_edit reports a drop with the template provider
    monkeypatch.setattr(run, "make_edit", lambda *a, **k: Edit(False, "The editor could not verify the story.", None,
                                                               None, template_meta("", "", 0), "template"))
    process_clip(db, s, _row(db, cid))
    row = _cands(ldb)[cid]
    assert _row(db, cid)["state"] == "dropped" and row["kept"] is None and row["explore"] == 0
    assert flag(ldb, "explore_at") is None                             # no editor call was spent on a test of the bar


def test_private_upload_is_not_a_post(clip, ldb, monkeypatch):
    s, db, cid, raw, calls = clip
    learn.add_candidate(ldb, cid, "twitch:kaicenat", "G", "funny", 9.0, HALF, 1000.0)
    monkeypatch.setattr(run, "upload_short", lambda yt, video, meta, credit, privacy: ("vid1", "private"))
    process_clip(db, s, _row(db, cid))
    row = _cands(ldb)[cid]
    assert _row(db, cid)["state"] == "uploaded" and row["kept"] == 1       # the verdict is recorded
    assert row["video_id"] is None and row["posted_at"] is None            # but a private upload is not a post


def test_learned_bar_replaces_min_rank(candidates, ldb, monkeypatch):
    s, db, ids = candidates
    monkeypatch.setattr(run.jev, "ask", _jev([0.5, 0.5, 0.5]))
    rows = run.cut_rows(db, 1100.0, 3600)
    _learned_state(ldb, min_rank=0.60)
    assert ORIGINAL_CHOOSE(db, s, rows) == []                              # 0.50 is under the learned 0.60
    _learned_state(ldb)
    assert sorted(r["id"] for r in ORIGINAL_CHOOSE(db, s, rows)) == sorted(ids)   # without it, jev.MIN_RANK 0.45


def test_learned_fast_lane(candidates, ldb, monkeypatch):
    s, db, ids = candidates
    monkeypatch.setattr(run.jev, "ask", _jev([0.52, 0.2, 0.2]))
    process = lambda db, s, row: set_state(db, row["id"], "uploaded", video_id="v", uploaded_at=1100.0)
    assert run.prep_once(db, s, process=process, now=lambda: 1100.0) is None     # 0.52 is under the set 0.8
    _learned_state(ldb, fast_lane=0.50)
    assert run.prep_once(db, s, process=process, now=lambda: 1100.0) == ids[0]


def test_learn_false_ignores_state(candidates, ldb, monkeypatch):
    s, db, ids = candidates
    s = _replace(s, learn=False)
    monkeypatch.setattr(run.jev, "ask", _jev([0.5, 0.5, 0.5]))
    _learned_state(ldb, min_rank=0.60, weights={"hook": 0.40, "context": 0.12, "payoff": 0.12, "crowd": 0.12,
                                                "appeal": 0.12, "story": 0.12})
    assert sorted(r["id"] for r in ORIGINAL_CHOOSE(db, s, run.cut_rows(db, 1100.0, 3600))) == sorted(ids)
    assert sorted(_cands(ldb)) == sorted(ids)                              # still recorded
    assert run._bars(s) == (run.jev.MIN_RANK, s.fast_lane) and run._learned(s) == {}


def _more_clips(tmp_path, db, n):
    """Further cut clips beside the `clip` fixture's (its spike is second 30; these are 31, 32, ...)."""
    ids = []
    for i in range(n):
        raw = tmp_path / "clips" / f"x{i}" / "raw.ts"; raw.parent.mkdir(parents=True); raw.write_bytes(b"x")
        (raw.parent / "context.json").write_text(json.dumps({"game": "G", "name": "", "chat": []}))
        ids.append(add_clip(db, "twitch:kaicenat", 1001.0 + i, 1011.0 + i, 5.0, "funny", ["LUL"], str(raw), 970.0))
    return ids


def test_exploration_goes_last_and_is_marked(clip, ldb, tmp_path, monkeypatch):
    s, db, cid, raw, calls = clip                         # cid ranks 0.40; the bar is 0.45, exploration reaches 0.35
    best, too_weak, second = _more_clips(tmp_path, db, 3)
    monkeypatch.setitem(run.SECRETS, "jev", "key")
    monkeypatch.setattr(run.jev, "ask", _jev({-60: 0.40, -59: 0.60, -58: 0.30, -57: 0.38}))   # by spike second minus 90
    _learned_state(ldb, min_rank=0.45)
    rows = run.cut_rows(db, 1100.0, 3600)
    assert [r["id"] for r in ORIGINAL_CHOOSE(db, s, rows)] == [best]       # not due: only what clears the bar
    assert run.EXPLORING == set()
    for i in range(9):                                                      # nine normal public posts since
        learn.add_candidate(ldb, f"p{i}", "twitch:a", "G", "funny", 4.0, HALF, 1000.0)
        learn.set_posted(ldb, f"p{i}", profile="primary", variant="box", video_id=f"v{i}", title="T", what="W",
                         context="C", reaction="R", length_s=30.0, posted_at=2000.0 + i)
    monkeypatch.setattr(run.time, "time", lambda: 1_000_000.0)
    assert [r["id"] for r in ORIGINAL_CHOOSE(db, s, rows)] == [best, cid]  # due: the 0.40 clip follows; 0.30 never
    assert run.EXPLORING == {cid} and too_weak not in run.EXPLORING       # and only the best below the bar: 0.38
    assert second not in run.EXPLORING                                     # would be a second editor call in one slot
    process_clip(db, s, _row(db, cid))                                     # processing the explored clip marks it
    row = _cands(ldb)[cid]
    assert row["explore"] == 1 and row["kept"] == 1 and flag(ldb, "explore_at") == "1000000.0"
    assert _cands(ldb)[best]["explore"] == 0


def test_learned_examples_reach_the_editor(clip, ldb, monkeypatch):
    s, db, cid, raw, calls = clip
    seen, fixture_edit = [], run.make_edit              # the fixture's fake takes no `examples`: record it, then drop it
    def spy(*a, **kw):
        seen.append(kw.pop("examples", "not passed")); return fixture_edit(*a, **kw)
    monkeypatch.setattr(run, "make_edit", spy)
    _learned_state(ldb, examples=["E1", "E2"])
    process_clip(db, s, _row(db, cid))
    set_state(db, cid, "cut")
    _learned_state(ldb)
    process_clip(db, s, _row(db, cid))
    assert seen == [["E1", "E2"], "not passed"]            # nothing learned: the editor's own call, as before


def test_learning_failure_changes_nothing(clip, tmp_path, monkeypatch):
    s, db, cid, raw, calls = clip
    other, _ = _more_clips(tmp_path, db, 2)
    monkeypatch.setitem(run.SECRETS, "jev", "key")
    monkeypatch.setattr(run.jev, "ask", _jev({-60: 0.40, -59: 0.60, -58: 0.30}))
    rows = run.cut_rows(db, 1100.0, 3600)
    monkeypatch.setattr(run, "LEARN", BrokenRecord())
    monkeypatch.setattr(run, "EXPLORING", set())
    broken = [r["id"] for r in ORIGINAL_CHOOSE(db, s, rows)]               # scored and chosen with a locked record
    run.EXPLORING.add(cid)                                                 # even a marked clip: the mark cannot be saved
    assert run._learned(s) == {} and run._bars(s) == (run.jev.MIN_RANK, s.fast_lane)
    process_clip(db, s, _row(db, cid))                                     # and posted
    assert _row(db, cid)["state"] == "uploaded" and _row(db, cid)["video_id"] == "vid1"
    assert calls["upload"][0] == GOOD["title"] and calls["render"] == (22.0, 44.0)
    monkeypatch.setattr(run, "LEARN", None)                                # cached scores: the same call without a record
    assert broken == [r["id"] for r in ORIGINAL_CHOOSE(db, s, rows)] == [other]
    assert run.get_scores(db, other)["rank"] == pytest.approx(0.60)


def _posted_at(ldb, vid, profile, at):
    learn.add_candidate(ldb, f"c-{vid}", "twitch:a", "G", "funny", 4.0, HALF, at)
    learn.set_posted(ldb, f"c-{vid}", profile=profile, variant="box", video_id=vid, title="T", what="W",
                     context="C", reaction="R", length_s=30.0, posted_at=at)


def _snapshots(ldb):
    return {(r["video_id"], r["age_h"]): (r["views"], r["likes"], r["taken"])
            for r in ldb.execute("select * from snapshots")}


def test_snapshots_are_taken_when_due(ldb, tmp_path, monkeypatch):
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    monkeypatch.setattr(run, "_youtube", lambda *a: pytest.fail("the learn loop must not sign in through _youtube"))
    _posted_at(ldb, "v1", "backup", 10_000.0)                              # two hours old
    asked = []
    def stats(yt, ids): asked.append((yt, list(ids))); return {"v1": (120, 7)}
    run.learn_once(s, open_db(tmp_path / "a.db"), now=lambda: 10_000.0 + 2 * 3600,
                   client=lambda profile: (f"yt-{profile}", "UC"), stats=stats)
    assert _snapshots(ldb) == {("v1", 1): (120, 7, 10_000.0 + 2 * 3600)}   # the 1 h row only; 6 h is not due
    assert asked == [("yt-backup", ["v1"])]
    _posted_at(ldb, "v2", "backup", 10_000.0)
    run.learn_once(s, None, now=lambda: 10_000.0 + 2 * 3600, client=lambda p: pytest.fail("the client is kept"),
                   stats=lambda yt, ids: {"v2": (1, 0)})                # a later pass reuses the client: no sign-in
    assert ("v2", 1) in _snapshots(ldb)
    run.learn_once(s, None, now=lambda: 10_000.0 + 2 * 3600, client=lambda p: pytest.fail("nothing is due"),
                   stats=lambda *a: pytest.fail("nothing is due"))         # nothing due: no YouTube call at all


def test_snapshot_failure_retries_without_pausing(ldb, tmp_path, monkeypatch):
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    db = open_db(tmp_path / "a.db")
    monkeypatch.setattr(run, "_youtube", lambda *a: pytest.fail("the learn loop must not sign in through _youtube"))
    _posted_at(ldb, "v1", "backup", 10_000.0); _posted_at(ldb, "v2", "primary", 10_000.0)
    now = lambda: 10_000.0 + 2 * 3600
    def down(profile):
        if profile == "backup": raise RuntimeError("sign-in failed")
        return "yt", "UC"
    stats = lambda yt, ids: {i: (50, 2) for i in ids}
    run.learn_once(s, db, now=now, client=down, stats=stats)
    assert set(_snapshots(ldb)) == {("v2", 1)}                             # backup's pass failed alone; primary went on
    assert paused(db, "backup") == "" and paused(db, "primary") == ""
    run.learn_once(s, db, now=now, client=lambda profile: ("yt", "UC"), stats=stats)
    assert set(_snapshots(ldb)) == {("v1", 1), ("v2", 1)}                  # the next pass retried it
    def quota(yt, ids): raise RuntimeError("quota")
    _posted_at(ldb, "v3", "backup", 10_000.0)
    run.learn_once(s, db, now=now, client=lambda profile: ("yt", "UC"), stats=quota)   # a failed request is skipped too
    assert ("v3", 1) not in _snapshots(ldb) and paused(db, "backup") == ""


def test_omitted_video_gets_no_snapshot(ldb, tmp_path):
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    _posted_at(ldb, "v1", "backup", 10_000.0)
    run.learn_once(s, open_db(tmp_path / "a.db"), now=lambda: 10_000.0 + 2 * 3600,
                   client=lambda profile: ("yt", "UC"), stats=lambda yt, ids: {})
    assert _snapshots(ldb) == {}


def _watched_setup(ldb, monkeypatch, *, has=True, watched=None):
    posted = 10_000.0
    _posted_at(ldb, "v1", "backup", posted); _posted_at(ldb, "v2", "backup", posted + 2 * 3600)
    asked = []
    monkeypatch.setattr(run.upload, "has_analytics", lambda profile: has)
    def fake(profile, ids, start, end):
        asked.append((profile, list(ids), start, end)); return watched(ids) if watched else {i: 61.5 for i in ids}
    monkeypatch.setattr(run.upload, "watched", fake)
    return asked, posted + 72 * 3600 + 3600


def _run_72h(tmp_path, now):
    s = Settings(streams=(), data_dir=tmp_path, volume=tmp_path)
    run.learn_once(s, open_db(tmp_path / "a.db"), now=lambda: now, client=lambda profile: ("yt", "UC"),
                   stats=lambda yt, ids: {i: (900, 30) for i in ids})


def _watched_rows(ldb):
    return {(r["video_id"], r["age_h"]): r["watched"] for r in ldb.execute("select * from snapshots")}


def test_72h_snapshot_records_watch_through(ldb, tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_youtube", lambda *a: pytest.fail("the learn loop must not sign in through _youtube"))
    asked, now = _watched_setup(ldb, monkeypatch)
    _run_72h(tmp_path, now)                                            # 1970-01-01 plus 3 days and 3 hours
    rows = _watched_rows(ldb)
    assert rows[("v1", 72)] == 61.5 and {v for k, v in rows.items() if k[1] != 72} <= {None}
    assert asked == [("backup", ["v1"], "1970-01-01", "1970-01-04")]   # only v1 is 72 h old; v2 is an hour short


def test_no_analytics_permission_asks_nothing(ldb, tmp_path, monkeypatch):
    asked, now = _watched_setup(ldb, monkeypatch, has=False)
    _run_72h(tmp_path, now)
    assert asked == [] and _watched_rows(ldb)[("v1", 72)] is None


@pytest.mark.parametrize("broken", ["has_analytics", "watched"])
def test_analytics_trouble_still_writes_the_snapshot(ldb, tmp_path, monkeypatch, broken):
    asked, now = _watched_setup(ldb, monkeypatch)
    def boom(*a): raise RuntimeError("analytics down")
    monkeypatch.setattr(run.upload, broken, boom)
    monkeypatch.setattr(run, "_youtube", lambda *a: pytest.fail("the learn loop must not sign in through _youtube"))
    _run_72h(tmp_path, now)
    assert _snapshots(ldb)[("v1", 72)][:2] == (900, 30) and _watched_rows(ldb)[("v1", 72)] is None


def test_video_missing_from_analytics_gets_no_watch_through(ldb, tmp_path, monkeypatch):
    asked, now = _watched_setup(ldb, monkeypatch, watched=lambda ids: {})
    _run_72h(tmp_path, now)
    assert _watched_rows(ldb)[("v1", 72)] is None and ("v1", 72) in _snapshots(ldb)


def test_a_single_profile_daemon_records_its_posting_profile_and_a_routed_one_does_not(tmp_path, monkeypatch):
    from autoclipper.store import posting_profile
    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    async def crash(*a, **k): raise RuntimeError("stop here")
    monkeypatch.setattr(run, "stream_task", crash)
    monkeypatch.setattr(run, "check_channels", lambda db, s: None)
    (tmp_path / "s.toml").write_text(f'data_dir="{tmp_path}"\nvolume="{tmp_path}"\nyoutube_profile="backup"\n'
                                     '[[stream]]\nplatform="twitch"\nchannel="a"\n')
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    db = open_db(tmp_path / "autoclipper.db")
    assert posting_profile(db) == "backup"
    (tmp_path / "s.toml").write_text(f'data_dir="{tmp_path}"\nvolume="{tmp_path}"\n'
                                     'variant_profiles = { box = "backup", emoji = "primary" }\n'
                                     '[[stream]]\nplatform="twitch"\nchannel="a"\n')
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    assert posting_profile(db) == "backup"                              # routing posts on several: kept as it was


# --- round 7: sign-ins never hold the posting lock and never wait long

def _fresh(db, cid, t=200000.0):
    """Move the clip fixture's cut to just before t, so the slot and fast-lane passes see it."""
    db.execute('update clips set onset = ?, "end" = ?, raw_t0 = ? where id = ?', (t - 100, t - 90, t - 130, cid))


def test_the_slot_signs_in_outside_the_posting_lock(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    _fresh(db, cid)
    monkeypatch.setattr(run.time, "time", lambda: 200000.0)
    held = []
    monkeypatch.setattr(run, "client", lambda profile: held.append(run.HEAVY.locked()) or ("yt", f"UC-{profile}"))
    assert asyncio.run(slot_once(s, db, now=lambda: 200000.0)) == cid
    assert held == [False] and _row(db, cid)["state"] == "uploaded"


def test_the_fast_lane_signs_in_outside_the_posting_lock(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    _fresh(db, cid)
    monkeypatch.setattr(run.time, "time", lambda: 200000.0)
    monkeypatch.setattr(run, "SECRETS", {"jev": "key", "twitch": None, "ok": True})
    monkeypatch.setattr(run, "score_clip", lambda db, s, row, a, key: 1.0)
    held = []
    monkeypatch.setattr(run, "client", lambda profile: held.append(run.HEAVY.locked()) or ("yt", f"UC-{profile}"))
    assert run.prep_once(db, s, now=lambda: 200000.0) == cid
    assert held == [False] and _row(db, cid)["state"] == "uploaded"


def test_the_companion_signs_in_outside_the_posting_lock(paired, monkeypatch):
    s, db, cid, raw, calls = paired
    process_clip(db, s, _row(db, cid))
    held = []
    monkeypatch.setattr(run, "client", lambda profile: held.append(run.HEAVY.locked()) or ("yt", f"UC-{profile}"))
    assert run.companion_once(db, s, now=LATER) == cid and held == [False]


def test_a_sign_in_that_does_not_answer_is_given_up_after_a_bounded_wait(clip, monkeypatch):
    import threading
    s, db, cid, raw, calls = clip
    release = threading.Event()
    monkeypatch.setattr(run, "client", lambda profile: release.wait(5) and ("yt", f"UC-{profile}"))
    monkeypatch.setattr(run, "SIGN_IN_WAIT_S", 0.2)
    started = run.time.monotonic()
    try:
        assert process_clip(db, s, _row(db, cid)) == run.WAITS
        assert run.time.monotonic() - started < 2 and _row(db, cid)["state"] == "cut"
        assert paused(db, "primary") == "" and "transcribed" not in calls
    finally:
        release.set()


def test_a_clip_taken_while_its_channel_signs_in_is_not_posted_again(clip, monkeypatch):
    s, db, cid, raw, calls = clip
    _fresh(db, cid)
    monkeypatch.setattr(run.time, "time", lambda: 200000.0)
    def client(profile):                                                 # another pass claims the clip meanwhile
        set_state(db, cid, "uploaded", video_id="elsewhere", uploaded_at=1.0)
        return "yt", f"UC-{profile}"
    monkeypatch.setattr(run, "client", client)
    assert asyncio.run(slot_once(s, db, now=lambda: 200000.0)) is None
    assert "upload" not in calls and _row(db, cid)["video_id"] == "elsewhere"


def test_startup_starts_posting_without_waiting_for_every_channel_to_sign_in(tmp_path, monkeypatch):
    import threading
    _toml(tmp_path)
    monkeypatch.setattr(run.subprocess, "Popen", FakePopen)
    order, posting = [], threading.Event()
    def slow_check(db, s):
        order.append("sign-ins answered" if posting.wait(2) else "sign-ins timed out")
    monkeypatch.setattr(run, "check_channels", slow_check)
    async def slots(s, db):
        order.append("posting started"); posting.set()
        raise RuntimeError("stop here")
    monkeypatch.setattr(run, "_slot_loop", slots)
    async def forever(*a, **k): await asyncio.Event().wait()
    monkeypatch.setattr(run, "stream_task", forever)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(run.main(tmp_path / "s.toml"))
    assert order[0] == "posting started"
