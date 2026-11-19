import asyncio
import fcntl
import json
import logging
import logging.handlers
import os
import sys
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from collections import Counter
from dataclasses import dataclass, replace

from autoclipper import discover as disc
from autoclipper import instagram, jev, learn, studio, upload
from autoclipper.capture import SEG_S, covered_window, cut, has_settled_tail, pipeline_argv, prune
from autoclipper.chat import open_chat
from autoclipper.config import (MAX_STREAMS, Discover, Settings, StreamCfg, disk_ok,
                                instagram_profiles, learn_path, load, profile_for, profiles)
from autoclipper.detect import Detector, _norm
from autoclipper.meta import STORY_MIN_S, Meta, emoji_title, make_edit
from autoclipper.quality import detect as quality_check
from autoclipper.render import (WIDE, Word, build_overlay, extend_window, frames, landscape_thumbnail, reel_window,
                                render_landscape, render_reel, render_short, thumbnail, transcribe, trim_window)
from autoclipper.store import (add_clip, cut_rows, drop_pending, expire, flag, get_scores, last_upload,
                               lift, migrate_pause, next_output, open_db, pause, paused, pick_variant, put_output,
                               record_posting_profile, recover,
                               save_scores, set_output, set_state, uploaded_ids, uploads_since)
from autoclipper.upload import (QuotaExhausted, SignInError, UploadLimit, client, credit_line, definitive, describe,
                                http_failure, set_thumbnail, strike_check, upload_short, verified_channel,
                                video_stats, wait_processed)

BACKOFF = (5, 10, 20, 40, 60)
CAPTION_STYLE = {"box": "box", "emoji": "stroke"}   # burned-in caption style of each presentation variant
HEALTHY_S = 300                    # capture this long resets the backoff
CUT_DELAY_S = 90                   # maximum wait from detection if the end of the cut has not settled
CUT_EXTRA_S = 5                    # the raw clip starts at least this much before the fallback trim's pre-roll
PRE_S, POST_S = 90, 30             # raw footage around the event for a short edit
STORY_PRE_S, STORY_POST_S = 240, 60 # source context for a full 1–5 minute story
CHAT_KEEP_S = 600                  # per-second chat kept for the raw cut's timeline
PREP_EVERY_S = 15                  # new cuts are prepared and scored this soon, so the fast lane can post at once
MAX_FROZEN = MAX_SILENT = 0.5      # a raw cut more frozen or silent than this is dropped before any model sees it
YOUTUBE_LAG_S = 15                 # YouTube chat arrives in batches up to 12 s late
CHAT_RETRY_S = (5, 60)             # first and longest wait before reopening chat
TICK_S = 1
DISK_EVERY_S = 60
GUARD_EVERY_S = 1800
SLOT_POLL_S = 60                   # shortest wait of the slot loop
GAME_EVERY_S = 300                 # how often a fixed stream's live category (game) is read again
GAME_STALE_S = 900                 # a game name no lookup has confirmed for this long is dropped
# YouTube's own broad video categories (streamlink reports one of these for a YouTube live), never a game's name
YOUTUBE_CATEGORIES = frozenset({
    "Film & Animation", "Autos & Vehicles", "Music", "Pets & Animals", "Sports", "Travel & Events", "Gaming",
    "People & Blogs", "Comedy", "Entertainment", "News & Politics", "Howto & Style", "Education",
    "Science & Technology", "Nonprofits & Activism"})
LIMIT_PAUSE_S = 10800              # a channel upload-limit error pauses that channel's uploads for 3 hours
SIGN_IN_WAIT_S = 30                # a posting pass gives up on a sign-in that has not answered by then
KEEP_CLIPS_S = 72 * 3600           # finished clip directories are removed after this
N_FRAMES = 4
PROBE_TIMEOUT_S = 120

log = logging.getLogger("autoclipper")
STOP = threading.Event()           # set on SIGTERM; process_clip returns its clip to cut at the next checkpoint
HEAVY = threading.Lock()           # one heavy job at a time: preparing, scoring, rendering and uploading a clip
CUT_TASKS: set[asyncio.Task] = set()   # cuts outlive a swapped-out stream; only shutdown cancels them
SECRETS = {"jev": None, "twitch": None, "ok": False}   # read at startup; "ok" false: the guard retries the read
BUFS: dict[str, dict] = {}         # buffer folder name -> its stream's state (active, pending cuts)
LEARN: sqlite3.Connection | None = None   # the learning record, opened in main; None: hand-set values only
EXPLORING: set[str] = set()        # clips choose_clips admitted below the bar; set under HEAVY, only read by process_clip
LEARN_EVERY_S = 600                # snapshots and learned values are refreshed this often
_FAILED: set[str] = set()          # learning failures already logged, so a broken record is reported once
STATS_CLIENTS: dict[str, object] = {}   # profile -> the API client the learn loop keeps, so a pass costs one request


def _failed(what: str, error: Exception) -> None:
    """A learning call failed: log it once and carry on with the hand-set values."""
    if (note := f"{what}: {type(error).__name__}: {error}") not in _FAILED:
        _FAILED.add(note)
        log.warning("learning %s; hand-set values apply", note)


def _learned(s: Settings) -> dict:
    """The learned weights, bars and examples; {} (the hand-set values) when off, empty or failing."""
    if LEARN is None or not s.learn:
        return {}
    try:
        return learn.state(LEARN)
    except Exception as e:
        _failed("state", e)
        return {}


def _bars(s: Settings) -> tuple[float, float]:
    """(slot minimum, post at once): learned, else jev.MIN_RANK and the configured fast_lane."""
    got = _learned(s)
    return got.get("min_rank", jev.MIN_RANK), got.get("fast_lane", s.fast_lane)


def _record(fn, *args, **kwargs) -> None:
    """Write to the learning record; a failure is logged and never reaches the posting path."""
    if LEARN is None:
        return
    try:
        fn(LEARN, *args, **kwargs)
    except Exception as e:
        _failed(fn.__name__, e)


def clips_root(s: Settings) -> Path:
    return s.clips_dir or s.data_dir / "clips"


def _safe(key: str) -> str:
    return key.replace(":", "-").replace("/", "-")


class Capture:
    """The streamlink | ffmpeg pair, stopped by handle only."""

    def __init__(self, procs: list[subprocess.Popen]):
        self.procs = procs

    def poll(self):
        codes = [p.poll() for p in self.procs]
        return None if all(c is None for c in codes) else next(c for c in codes if c is not None)

    def terminate(self) -> None:
        for p in self.procs:                       # streamlink first, so ffmpeg sees EOF and closes its segment
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def spawn_capture(cfg: StreamCfg, s: Settings) -> Capture:
    buf = s.data_dir / "buf" / _safe(cfg.key)
    epoch = int(time.time())
    while (buf / str(epoch)).exists():             # one directory per capture start, so timelines never mix
        epoch += 1
    run_dir = buf / str(epoch)
    run_dir.mkdir(parents=True)
    (s.data_dir / "logs").mkdir(exist_ok=True)
    errors = open(s.data_dir / "logs" / f"{_safe(cfg.key)}.capture.log", "ab")
    sl, ff = pipeline_argv(cfg.url, run_dir, s.ffmpeg)
    try:
        a = subprocess.Popen(sl, stdout=subprocess.PIPE, stderr=errors)
        try:
            b = subprocess.Popen(ff, stdin=a.stdout, stderr=errors)
        except BaseException:
            a.terminate()
            a.wait(10)
            raise
        finally:
            a.stdout.close()                       # ffmpeg owns the pipe now
    finally:
        errors.close()
    return Capture([a, b])


async def _read_chat(cfg: StreamCfg, chat, state: dict) -> None:
    """Chat never stops capture: any end or error is logged and the source is reopened with backoff."""
    wait = CHAT_RETRY_S[0]
    while True:
        try:
            async for msg in chat(cfg):
                state["last_chat"], wait = time.time(), CHAT_RETRY_S[0]
                book, sec = state["chat"], int(msg.ts)
                if sec not in book:                 # a new second: forget seconds older than CHAT_KEEP_S
                    for old in [k for k in book if k < sec - CHAT_KEEP_S]:
                        del book[old]
                n_top = book.setdefault(sec, [0, Counter()])     # per-second timeline for the editor
                n_top[0] += 1
                n_top[1][_norm(msg.text)] += 1
                if state["det"] is not None:
                    state["det"].feed(msg)
            log.info("%s: chat ended", cfg.key)
        except Exception:
            log.exception("%s: chat failed", cfg.key)
        await asyncio.sleep(wait)
        wait = min(wait * 2, CHAT_RETRY_S[1])


async def _wait_for_buffer(buf: Path, end: float, max_wait: float) -> bool:
    deadline = time.monotonic() + max_wait
    while (remaining := deadline - time.monotonic()) > 0:
        until_end = end - time.time()
        if until_end <= 0 and await asyncio.to_thread(has_settled_tail, buf, end):
            return True
        await asyncio.sleep(min(remaining, until_end if until_end > 0 else SEG_S))
    return False


async def _cut_later(cfg: StreamCfg, s: Settings, db, buf: Path, ev, state: dict) -> None:
    try:
        pre, post = (STORY_PRE_S, STORY_POST_S) if s.narrative else (PRE_S, POST_S)
        end = ev.end + post
        settled = await _wait_for_buffer(buf, end, CUT_DELAY_S + post - POST_S)
        if not disk_ok(s):
            log.warning("%s: data volume missing or full; event at %.0f not cut", cfg.key, ev.onset)
            return
        out_dir = clips_root(s) / f"{_safe(cfg.key)}-{int(ev.onset)}"
        out_dir.mkdir(parents=True, exist_ok=True)
        start = ev.onset - max(pre, cfg.pre_roll_s + CUT_EXTRA_S)
        raw = await asyncio.to_thread(cut, buf, start, end, out_dir / "raw.ts", s.ffmpeg)
        if raw is None:
            window = await asyncio.to_thread(covered_window, buf, start, end,
                                             core_start=ev.onset - min(cfg.pre_roll_s, 15), core_end=ev.end + 5)
            if window is not None and window != (start, end):
                raw = await asyncio.to_thread(cut, buf, *window, out_dir / "raw.ts", s.ffmpeg)
                if raw is not None:
                    log.info("%s: event at %.0f kept with %.1fs less surrounding footage",
                             cfg.key, ev.onset, (end - start) - (window[1] - window[0]))
        if raw is None:
            log.info("%s: event at %.0f not cut (gap or not buffered)", cfg.key, ev.onset)
            try:
                out_dir.rmdir()
            except OSError:
                pass
            return
        chat = [{"s": int(sec - raw.t0), "n": n, "top": [m for m, _ in top.most_common(3)]}
                for sec, (n, top) in sorted(state["chat"].items()) if raw.t0 <= sec < raw.t0 + raw.duration]
        (out_dir / "context.json").write_text(json.dumps({"game": state.get("game", ""), "name": cfg.name,
                                                          "chat": chat, "trigger": ev.trigger}))
        clip_id = add_clip(db, cfg.key, ev.onset, ev.end, ev.score, ev.mood, ev.sample, str(raw.path), raw.t0)
        log.info("%s: clip cut, onset %.0f score %.1f %s; id %s end-to-cut %.1fs (%s)",
                 cfg.key, ev.onset, ev.score, ev.mood, clip_id, time.time() - ev.end,
                 "settled buffer" if settled else "wait limit")
    except Exception:
        log.exception("%s: cut failed for the event at %.0f", cfg.key, ev.onset)
    finally:
        state["pending"] -= 1
        if not state.get("active") and state["pending"] == 0 and BUFS.get(buf.name) is state:
            _drop_buffer(buf)                       # the stream is gone and this was its last cut
            BUFS.pop(buf.name, None)


def _drop_buffer(d: Path) -> None:
    """Remove a retired stream's run folders, without following any symlink."""
    for run_dir in list(d.iterdir()) if d.is_dir() and not d.is_symlink() else []:
        if run_dir.is_dir() and not run_dir.is_symlink():
            shutil.rmtree(run_dir)
    try:
        d.rmdir()
    except OSError:
        pass


def sweep_buffers(s: Settings, now: float) -> None:
    """Every folder under buf/: a live stream's is pruned to the last 10 minutes; one with no live stream and no
    cut pending is removed. Stays inside buf/ and never follows a symlink."""
    root = s.data_dir / "buf"
    if root.is_symlink() or not root.is_dir():
        return
    for d in list(root.iterdir()):
        if d.is_symlink() or not d.is_dir() or d.resolve().parent != root.resolve():
            continue
        st = BUFS.get(d.name)
        if st is not None and st.get("active"):
            prune(d, now)
        elif st is None or st.get("pending", 0) == 0:
            _drop_buffer(d)
            BUFS.pop(d.name, None)


def _publish(db, cfg: StreamCfg, state: dict, t: float) -> None:
    try:
        state["events"] = [e for e in state["events"] if e > t - 3600]
        flag(db, f"stream:{cfg.key}", json.dumps({"capture": state["det"] is not None,
                                                  "last_chat": state["last_chat"],
                                                  "events": state["events"], "updated": t}))
    except Exception:
        log.exception("%s: status not written", cfg.key)


async def stream_task(cfg: StreamCfg, s: Settings, db, *, spawn=spawn_capture, chat=open_chat,
                      sleep=asyncio.sleep, now=time.time, state: dict | None = None) -> None:
    """Capture, chat and detection for one stream. `state` is shared with the discovery supervisor, which reads
    whether the stream is busy (an open event or a cut pending) before swapping it out."""
    buf = s.data_dir / "buf" / _safe(cfg.key)
    state = state if state is not None else {}
    for k, v in (("det", None), ("last_chat", None), ("events", []), ("chat", {}), ("pending", 0), ("game", "")):
        state.setdefault(k, v)
    state["active"] = True
    BUFS[buf.name] = state
    reader = asyncio.create_task(_read_chat(cfg, chat, state))
    attempt = 0
    try:
        while True:
            if not disk_ok(s):
                log.warning("%s: data volume missing or full; capture paused", cfg.key)
                _publish(db, cfg, state, now())
                await sleep(60)
                continue
            proc, started = None, now()
            try:                                   # anything failing in one cycle is handled like a capture exit
                buf.mkdir(parents=True, exist_ok=True)     # only after disk_ok, so never on the internal disk
                proc = spawn(cfg, s)
                state["det"] = Detector(cfg.key, lag_s=YOUTUBE_LAG_S if cfg.platform == "youtube" else 0)
                last_disk = started
                while proc.poll() is None:
                    await asyncio.sleep(TICK_S)
                    t = now()
                    ev = state["det"].tick(t)
                    if ev is not None:
                        state["events"].append(t)
                        state["pending"] += 1
                        task = asyncio.create_task(_cut_later(cfg, s, db, buf, ev, state))
                        CUT_TASKS.add(task)
                        task.add_done_callback(CUT_TASKS.discard)
                    prune(buf, t)
                    if int(t) % 10 == 0:
                        _publish(db, cfg, state, t)
                    if t - last_disk >= DISK_EVERY_S:
                        last_disk = t
                        if not disk_ok(s):
                            break
            except Exception:
                log.exception("%s: capture cycle failed", cfg.key)
            finally:
                state["det"] = None                # an open event is dropped with its capture
                if proc is not None:
                    await asyncio.to_thread(proc.terminate)    # a slow shutdown must not freeze the other streams
                _publish(db, cfg, state, now())
            if now() - started >= HEALTHY_S:
                attempt = 0
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            log.warning("%s: capture stopped; retrying in %ss", cfg.key, wait)
            await sleep(wait)
            attempt += 1
    finally:                                       # pending cuts keep running: a stopped stream's moments still count
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        state["active"] = False
        if state["pending"] == 0 and BUFS.get(buf.name) is state:
            _drop_buffer(buf)
            BUFS.pop(buf.name, None)
        else:
            try:
                prune(buf, now())                   # keep only what its pending cut can still need
            except OSError:
                pass


def _duration(media: Path, ffmpeg: str) -> float:
    p = subprocess.run([str(Path(ffmpeg).with_name("ffprobe")), "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(media)], capture_output=True, text=True, check=True,
                       timeout=PROBE_TIMEOUT_S)
    return float(p.stdout.strip())


def _save(db, row, cfg: StreamCfg, ctx: dict, edit, window: tuple[float, float], work: Path, variant: str,
          profile: str) -> None:
    """Uploads are off: keep the finished Short with everything needed to post it by hand."""
    scored = get_scores(db, row["id"])
    m = edit.meta
    (work / "meta.json").write_text(json.dumps({
        "title": m.title, "hook": m.hook, "description": m.description, "tags": list(m.tags),
        "what_happened": edit.what_happened, "streamer": cfg.display, "url": cfg.url, "game": ctx.get("game", ""),
        "window": list(window), "provider": edit.provider, "variant": variant,
        "scores": None if scored is None else {"rank": scored["rank"], **json.loads(scored["answers"]).get("scores", {})},
    }, indent=1))
    set_state(db, row["id"], "saved", title=m.title, provider=edit.provider, uploaded_at=time.time(), variant=variant,
              youtube_profile=profile)
    log.info("%s: saved (uploads off): %s", row["id"], m.title)


def _slim(work: Path) -> None:
    """After an upload only the rendered videos and thumbnails stay; the raw cut, overlays and frames go."""
    (work / "raw.ts").unlink(missing_ok=True)
    for f in work.glob("frame_*.jpg"):
        f.unlink(missing_ok=True)
    for name in ("overlay", "overlay_wide", "overlay_reel"):
        overlay = work / name
        if overlay.is_dir() and not overlay.is_symlink():
            shutil.rmtree(overlay)


def _note(work: Path, fmt: str, fields: dict, **top) -> None:
    """Merge one format's review record (title, description, video, thumbnail, window, YouTube ID) into the clip
    folder's meta.json under "outputs", and any top-level fields (variant), keeping everything else in it."""
    path = work / "meta.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data.update(top)
    data.setdefault("outputs", {}).setdefault(fmt, {}).update(fields)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


QUOTA = "API quota exhausted"


def _youtube(db, profile: str):
    """(client, why, channel): an API client for a profile and the channel ID verified when it was built, which the
    caller keeps for its Studio fallback; or (None, why, channel). Only an identified credential failure (a
    definitive refresh error, a 401, a 403 whose reason is about the sign-in's rights, or a SignInError) pauses the
    profile (auth, once, with a notice). Anything else (the network, quota, 429, 5xx, a rate or limit reason, an
    unknown reason, an unexpected error) pauses nothing: the caller puts the clip back and the next pass tries
    again. With the quota gone (why == QUOTA) a channel verified earlier still posts through Studio. A profile that
    resolves to the same channel as another one pauses both: one channel must never get both variants."""
    try:
        yt, channel = client(profile)
    except QuotaExhausted:
        return None, QUOTA, verified_channel(profile)
    except RefreshError as e:
        if not definitive(e):                     # a blip at Google or on the way there
            return None, f"sign-in refresh failed: {e}", None
        why = f"sign-in no longer valid: {e}"
    except HttpError as e:
        kind, reason = http_failure(e)
        if kind == "quota":
            return None, QUOTA, verified_channel(profile)
        if kind == "transient":                   # busy, rate-limited, down, or a reason we do not know
            log.warning("%s: YouTube answered %s (%s) while signing in", profile, e.resp.status, reason or "no reason")
            return None, f"YouTube {e.resp.status} {reason}".strip(), None
        why = f"YouTube refused the sign-in ({e.resp.status} {reason}): {e}"
    except SignInError as e:
        why = str(e)
    except Exception as e:                        # network (DNS, timeouts, resets) or anything unexpected
        return None, f"cannot sign in now: {type(e).__name__}: {e}", None
    else:
        if _record_channel(db, profile, channel):
            return yt, "", channel
        return None, "same channel as another profile", None
    if pause(db, profile, "auth"):                # once; a strike already there stays and needs no second notice
        notify(f"Uploads on {profile} paused: YouTube authorisation failed. "
               f"Run `autoclipper auth --profile {profile}`, then `autoclipper resume --profile {profile}`.")
    return None, why, None


def _record_channel(db, profile: str, channel: str | None) -> bool:
    """Remember the channel a profile just signed in to (any sign-in: posting, startup or the guard). False, and
    every profile on that channel paused, when another profile already resolved to the same channel."""
    if channel is None:
        return True
    with CHANNEL_LOCK:                             # sign-ins run in several threads (posting, startup, the guard)
        twins = [p for p, c in CHECKED.items() if p != profile and c == channel]
        CHECKED[profile] = channel
        if not twins:
            return True
        changed = [pause(db, p, f"duplicate:{channel}") for p in (profile, *twins)]
    log.error("profiles %s all sign in to channel %s; their uploads are paused", [profile, *twins], channel)
    if any(changed):
        notify(f"Uploads on {', '.join([profile, *twins])} paused: they post to the same YouTube channel "
               f"{channel}. Fix the sign-ins, then `autoclipper resume`.")
    return False


CHECKED: dict[str, str | None] = {}   # profile -> channel ID, of each profile signed in so far in this process
CHANNEL_LOCK = threading.Lock()


def _hold(db, profile: str) -> str:
    """Why an upload on this profile must not start right now (shutting down, or any pause), or "". Read again
    immediately before every upload call, after any rendering."""
    if STOP.is_set():
        return "shutting down"
    why = paused(db, profile)
    return f"uploads on {profile} paused ({why})" if why else ""


def _sign_ins(db, wanted: Sequence[str]) -> dict[str, tuple]:
    """Sign in to each wanted profile at once, never under HEAVY: each _youtube runs in its own daemon thread and
    the caller waits at most SIGN_IN_WAIT_S in all. A sign-in that has not answered by then counts as a transient
    failure for this pass (its thread may still finish and record what it finds); a daemon thread never holds up
    shutdown."""
    results: dict[str, tuple] = {}
    threads = [threading.Thread(target=lambda p=p: results.__setitem__(p, _youtube(db, p)), daemon=True,
                                name=f"sign-in {p}") for p in dict.fromkeys(wanted)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + SIGN_IN_WAIT_S
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return {p: results.get(p, (None, f"no answer in {SIGN_IN_WAIT_S} s", None)) for p in dict.fromkeys(wanted)}


def _postable_profiles(db, s: Settings, t: float) -> list[str]:
    """The profiles a posting pass may need a client for."""
    return [p for p in profiles(s) if post_ok(db, s, t, p)] if s.upload else []


def check_channels(db, s: Settings) -> None:
    """With routing, at startup, beside the posting loops: sign in each profile in use once, so a profile that
    cannot sign in is paused (auth) and two profiles on one channel are paused early."""
    if not (s.upload and s.variant_profiles):
        return
    for profile, (yt, why, _) in _sign_ins(db, profiles(s)).items():
        if yt is None:
            log.warning("startup: %s not ready (%s)", profile, why)


def _thumb(yt, s: Settings, profile: str, video_id: str, jpg: Path, via_api: bool) -> str | None:
    """Set an uploaded video's thumbnail (API once processed, else Studio). Returns the error text on failure."""
    try:
        if not (via_api and wait_processed(yt, video_id) and set_thumbnail(yt, video_id, jpg)):
            with studio.context(s.chrome_profile(profile)) as ctx:
                studio.studio_set_thumbnail(ctx, video_id, jpg)
        return None
    except Exception as e:                         # the video is up; only its thumbnail is missing
        log.exception("%s: thumbnail not set", video_id)
        return f"thumbnail: {type(e).__name__}: {e}"[:500]


def _landscape(db, s: Settings, row, cfg: StreamCfg, edit, words: list[Word], window: tuple[float, float],
               raw_s: float, thumb_at: float, variant: str) -> None:
    """The 16:9 regular-video companion of an edited Short: the same raw cut, transcript and editor metadata, the
    window a little longer (extend_window), the full frame with captions, and its own thumbnail. It is queued for
    companion_once (uploads on) or kept as saved (uploads off). A failure is recorded on the companion alone."""
    clip_id, raw = row["id"], Path(row["raw_path"])
    work, meta, profile = raw.parent, edit.meta, profile_for(s, variant)
    credit = credit_line(cfg.display, cfg.url)
    story = edit.story
    details = ({"context": story.context, "what_happened": edit.what_happened, "reaction": story.reaction,
                "event_at": story.event_at} if story else {})
    _note(work, "short", {"title": meta.title, "description": describe(meta, credit), "video": str(work / "short.mp4"),
                          "thumb": str(work / "thumb.jpg"), "window": list(window), "video_id": None,
                          "state": "rendered" if s.upload else "saved", **details}, variant=variant)
    start, end = (story.start, story.end) if story else extend_window(words, *window, raw_s)
    video, jpg = work / "landscape.mp4", work / "landscape.jpg"
    info = {"title": meta.title, "hook": meta.hook, "description": meta.description, "tags": list(meta.tags),
            "credit": credit, "provider": meta.provider, "window": [start, end], "privacy": s.privacy}
    review = {"title": meta.title, "description": describe(meta, credit, short=False), "video": str(video),
              "thumb": str(jpg), "window": [start, end], "video_id": None, **details}
    try:
        if STOP.is_set():
            raise RuntimeError("shutting down")
        overlay = build_overlay(words, start, end, "", work / "overlay_wide", WIDE, style=CAPTION_STYLE[variant])
        render_landscape(raw, start, end, overlay, video, s.ffmpeg)
        landscape_thumbnail(raw, min(max(thumb_at, start), end - 0.1), meta.hook, jpg, s.ffmpeg)
    except Exception as e:
        error = f"landscape: {type(e).__name__}: {e}"[:500]
        log.exception("%s: landscape companion not rendered", clip_id)
        put_output(db, clip_id, "landscape", "failed", info, str(video), str(jpg), profile, error=error)
        _note(work, "landscape", {**review, "state": "failed", "error": error})
        return
    state = "pending" if s.upload else "saved"
    extra = {} if s.upload else {"uploaded_at": time.time()}
    if put_output(db, clip_id, "landscape", state, info, str(video), str(jpg), profile, **extra):
        _note(work, "landscape", {**review, "state": state})
        log.info("%s: landscape companion %s (%.1f to %.1f s)", clip_id, state, start, end)


def companion_once(db, s: Settings, *, now=time.time) -> str | None:
    """Post the oldest waiting landscape companion whose Short is up, as a regular video on the Short's profile and
    privacy, once post_ok allows it on that profile (pause, daily cap, min_gap_minutes after the Short). Its row is
    marked uploading first and uploaded the moment YouTube returns an ID, so it is never posted twice."""
    if not (s.upload and s.landscape) or STOP.is_set():
        return None
    waiting = [p for p in _postable_profiles(db, s, now()) if next_output(db, p, now() - KEEP_CLIPS_S) is not None]
    if not waiting:
        return None
    clients = _sign_ins(db, waiting)                # outside HEAVY, bounded
    with HEAVY:
        if STOP.is_set():
            return None
        for profile in waiting:                     # a channel that cannot post now never holds up the next one
            if not post_ok(db, s, now(), profile):  # rechecked when claiming
                continue
            out = next_output(db, profile, now() - KEEP_CLIPS_S)
            if out is None:
                continue
            yt, why, channel_id = clients[profile]
            if yt is None and not (why == QUOTA and channel_id):
                log.info("%s: landscape companion waits (%s)", out["clip_id"], why)
                continue
            if STOP.is_set():
                return None
            if not paused(db, profile):
                break
        else:
            return None
        clip_id = out["clip_id"]
        m = json.loads(out["meta"])
        privacy = m.get("privacy", s.privacy)
        meta = Meta(m["title"], m["hook"], m["description"], tuple(m["tags"]), 0, m["provider"])
        video, jpg = Path(out["video_path"]), Path(out["thumb_path"])
        if _hold(db, profile):
            return None
        set_output(db, clip_id, "landscape", "uploading")
        via_api = True
        try:
            try:
                if yt is None:
                    raise QuotaExhausted()
                video_id, confirmed = upload_short(yt, video, meta, m["credit"], privacy, short=False)
            except QuotaExhausted:                  # Studio opens the channel this client was verified with
                if _hold(db, profile) or channel_id is None:
                    set_output(db, clip_id, "landscape", "pending")
                    return None
                log.warning("%s: API quota exhausted; uploading the landscape companion through Studio", clip_id)
                with studio.context(s.chrome_profile(profile)) as ctx:
                    video_id = studio.studio_upload(ctx, video, meta, m["credit"], privacy, channel_id=channel_id,
                                                    short=False)
                via_api, confirmed = False, privacy     # Studio sets the privacy it was given
        except UploadLimit:
            pause(db, profile, f"limit:{int(time.time()) + LIMIT_PAUSE_S}")
            log.warning("%s: channel upload limit; uploads on %s paused for 3 hours", clip_id, profile)
            set_output(db, clip_id, "landscape", "pending")
            return None
        except Exception as e:                     # the Short is untouched; the companion is kept for inspection
            log.exception("%s: landscape companion failed", clip_id)
            set_output(db, clip_id, "landscape", "failed", error=f"{type(e).__name__}: {e}"[:500])
            return None
        set_output(db, clip_id, "landscape", "uploaded", video_id=video_id, uploaded_at=time.time(),
                   meta=json.dumps({**m, "uploaded_privacy": confirmed}))
        log.info("%s: landscape companion uploaded as %s", clip_id, video_id)
        error = _thumb(yt, s, profile, video_id, jpg, via_api)
        if error:
            set_output(db, clip_id, "landscape", "uploaded", error=error)
        try:
            _note(video.parent, "landscape", {"state": "uploaded", "video_id": video_id})
        except OSError:
            log.exception("%s: meta.json not updated", clip_id)
        return clip_id


WAITS = "waits"                     # process_clip put the clip back to cut: its channel could not take it now


def process_clip(db, s: Settings, row, clients: dict[str, tuple] | None = None) -> str | None:
    """Edit, render and post one cut. `clients` are the pass's sign-ins (made outside HEAVY); without them each
    channel is signed in here, bounded by SIGN_IN_WAIT_S."""
    clip_id = row["id"]

    def back_to_cut(why: str) -> str:
        set_state(db, clip_id, "cut")
        log.info("%s: back to cut (%s)", clip_id, why)
        return WAITS

    try:
        if s.upload:                                # a Reel queued by an earlier attempt waits for this attempt's
            drop_pending(db, clip_id, instagram.FORMAT)   # own Reel file and its own confirmed public Short
        # The variant decides the channel, so it comes first: the least-posted variant among those whose channel can
        # post now, then the others if its channel cannot sign in (signed in before any transcription or LLM spend).
        # A clip put back after its upload started keeps the variant it was given and waits for its channel.
        allowed = postable(db, s, time.time())
        routes = {v: profile_for(s, v) for v in s.caption_variants} if s.variant_profiles else None
        if row["variant"] in s.caption_variants:
            choices = [row["variant"]] if row["variant"] in allowed else []
        else:
            first = pick_variant(db, allowed, routes)
            choices = [first, *(v for v in allowed if v != first)] if first else []
        if not choices:
            return back_to_cut(f"{profile_for(s, row['variant'])} cannot post now" if row["variant"] in s.caption_variants
                               else "no channel can post now")
        failed = []
        for variant in choices:
            profile, yt, channel_id = profile_for(s, variant), None, None
            if not s.upload:                        # no YouTube at all when uploads are off
                break
            yt, why, channel_id = (clients.get(profile, (None, "not signed in for this pass", None))
                                   if clients is not None else _sign_ins(db, [profile])[profile])
            if yt is not None or (why == QUOTA and channel_id):   # without quota, a verified channel uses Studio
                break
            failed.append(f"{profile}: {why}")
        else:                                       # no channel could sign in: nothing recorded, the next pass retries
            return back_to_cut("; ".join(failed))
        raw = Path(row["raw_path"])
        work = raw.parent
        ctx = _context(work)
        cfg = stream_cfg(s, row["stream"], ctx.get("name", ""))
        if STOP.is_set():
            return back_to_cut("shutting down")
        set_state(db, clip_id, "rendering")
        a = prepare(db, s, row)
        if a is None:
            return                                  # dropped as frozen or silent
        if s.narrative and a["raw_s"] < STORY_MIN_S:
            set_state(db, clip_id, "dropped", error="less than 60 seconds of usable footage for the story")
            log.info("%s: dropped, only %.1fs of continuous source footage", clip_id, a["raw_s"])
            return
        words = [Word(*w) for w in a["words"]]
        onset_s, end_s = row["onset"] - row["raw_t0"], row["end"] - row["raw_t0"]
        if STOP.is_set():
            return back_to_cut("shutting down")
        editor_options = {"narrative": True} if s.narrative else {}
        if examples := _learned(s).get("examples"):
            editor_options["examples"] = examples
        edit = make_edit(db, time.strftime("%Y-%m-%d"), cfg.display, ctx.get("game", ""), cfg.platform, row["mood"],
                         words, chat_lines(ctx.get("chat", [])), onset_s, a["raw_s"], cwd=s.data_dir, **editor_options)
        if edit.provider != "template":             # a capped or failed editor never answered: not a verdict
            _record(learn.set_verdict, clip_id, edit.keep)
            if clip_id in EXPLORING:
                _record(learn.mark_explore, clip_id, time.time())
        if not edit.keep:
            set_state(db, clip_id, "dropped", error=f"editor: {edit.what_happened}"[:500])
            log.info("%s: the editor dropped it: %s", clip_id, edit.what_happened)
            return
        plain_title = edit.meta.title
        # The variant sets the caption style of every render and the title of every post; the hook stays plain.
        if variant == "emoji":
            edit = replace(edit, meta=replace(edit.meta, title=emoji_title(edit.meta.title, edit.emojis, row["mood"])))
        if edit.start is not None:
            start, end = edit.start, edit.end
        else:                                       # the editor failed or chose an unusable window
            start, end = trim_window(words, a["raw_s"], onset_s, end_s, pre_roll=cfg.pre_roll_s)
        meta = edit.meta
        if edit.story:
            log.info("%s: story %.1fs, Short %.1fs, event at %.1fs of source",
                     clip_id, edit.story.end - edit.story.start, end - start, edit.story.event_at)
        if STOP.is_set():
            return back_to_cut("shutting down")
        overlay = build_overlay(words, start, end, meta.hook, work / "overlay", style=CAPTION_STYLE[variant])
        video = render_short(raw, start, end, overlay, work / "short.mp4", s.ffmpeg)
        thumbnail_event = edit.story.event_at if edit.story else onset_s
        thumb_at = min(max(thumbnail_event + 1.0, start), end - 0.1)
        thumb = thumbnail(raw, thumb_at, meta.hook, work / "thumb.jpg", s.ffmpeg)
        if not s.upload:
            _save(db, row, cfg, ctx, edit, (start, end), work, variant, profile)
        if s.landscape and not STOP.is_set():
            try:
                _landscape(db, s, row, cfg, edit, words, (start, end), a["raw_s"], thumb_at, variant)
            except Exception:
                log.exception("%s: landscape companion not queued", clip_id)
        if not s.upload:
            return _slim(work)
        if STOP.is_set():
            return back_to_cut("shutting down")
        if paused(db, profile):                   # the guard may have paused uploads while this clip rendered
            return back_to_cut(f"uploads on {profile} paused")
        credit = credit_line(cfg.display, cfg.url)
        if s.instagram_account_id and s.privacy == "public" and profile in instagram_profiles(s):
            try:                                    # Instagram fails Reels near a minute: a longer Short gets a cut
                reel_start, reel_end = reel_window(start, end, thumbnail_event)
                reel = video
                if (reel_start, reel_end) != (start, end):
                    reel_overlay = build_overlay(words, reel_start, reel_end, meta.hook, work / "overlay_reel",
                                                 style=CAPTION_STYLE[variant])
                    reel = render_reel(raw, reel_start, reel_end, reel_overlay, work / "reel.mp4", s.ffmpeg)
                if not (reel.is_file() and reel.stat().st_size > 0):
                    raise RuntimeError(f"no Reel file at {reel}")
                cover = min(max(thumb_at, reel_start), reel_end - 0.1) - reel_start
                instagram.queue(db, clip_id, profile, s.instagram_account_id, reel, thumb,
                                meta.title, meta.description, credit, round(cover * 1000))
                _note(work, instagram.FORMAT, {"video": str(reel), "window": [reel_start, reel_end]})
            except Exception:
                log.exception("%s: Instagram Reel not queued", clip_id)
        if why := _hold(db, profile):              # again: the Reel render may have taken a while
            return back_to_cut(why)
        set_state(db, clip_id, "uploading", title=meta.title, provider=meta.provider,
                  youtube_profile=profile, variant=variant, privacy=None)
        via_api = True
        try:
            if yt is None:
                raise QuotaExhausted()
            video_id, confirmed = upload_short(yt, video, meta, credit, s.privacy)
        except QuotaExhausted:                      # Studio opens the channel this client was verified with
            if why := _hold(db, profile):
                return back_to_cut(why)
            if channel_id is None:
                return back_to_cut(f"no verified channel of {profile} for Studio")
            log.warning("%s: API quota exhausted; uploading through Studio", clip_id)
            with studio.context(s.chrome_profile(profile)) as ctx:
                video_id = studio.studio_upload(ctx, video, meta, credit, s.privacy, channel_id=channel_id)
            via_api, confirmed = False, s.privacy        # Studio sets the privacy it was given
        except UploadLimit:
            pause(db, profile, f"limit:{int(time.time()) + LIMIT_PAUSE_S}")
            log.warning("%s: channel upload limit; uploads on %s paused for 3 hours", clip_id, profile)
            return back_to_cut("channel upload limit")
    except Exception as e:                         # nothing is deleted, so a failed clip can be inspected
        log.exception("%s: failed", clip_id)
        set_state(db, clip_id, "failed", error=f"{type(e).__name__}: {e}"[:500])
        return
    set_state(db, clip_id, "uploaded", video_id=video_id, uploaded_at=time.time(),   # recorded the moment it exists
              privacy=confirmed)
    if confirmed == "public":
        _record(learn.set_posted, clip_id, profile=profile, variant=variant, video_id=video_id, title=plain_title,
                what=edit.what_happened, context=edit.story.context if edit.story else "",
                reaction=edit.story.reaction if edit.story else "", length_s=end - start, posted_at=time.time())
    else:                                           # only a Short YouTube confirms public ever gets its Reel
        drop_pending(db, clip_id, instagram.FORMAT)
    log.info("%s: uploaded as %s (%s)", clip_id, video_id, meta.provider)
    if s.landscape:
        try:
            _note(work, "short", {"state": "uploaded", "video_id": video_id})
        except OSError:
            log.exception("%s: meta.json not updated", clip_id)
    # A Short's thumbnail can only be set once YouTube has processed the video (30 s for a 10 s test Short).
    error = _thumb(yt, s, profile, video_id, thumb, via_api)
    if error:
        set_state(db, clip_id, "uploaded", error=error)
    _slim(work)


def stream_cfg(s: Settings, key: str, name: str = "") -> StreamCfg:
    """The configured stream for a key, else a plain one (a discovered stream) with its display name."""
    named = (*s.streams, *((*s.discover.pin, *s.discover.prefer) if s.discover else ()))
    found = next((c for c in named if c.key == key), None)
    if found is not None:
        return found
    platform, _, channel = key.partition(":")
    return StreamCfg(platform, channel, name=name if not any(ch in name for ch in "@<>") else "")


def _context(work: Path) -> dict:
    try:
        return json.loads((work / "context.json").read_text())
    except (OSError, ValueError):
        return {}


def chat_lines(chat: list[dict]) -> list[str]:
    return [f"{c['s']}: {c['n']} msgs" + (f" | {', '.join(c['top'])}" if c.get("top") else "") for c in chat]


def prepare(db, s: Settings, row) -> dict | None:
    """Transcript, frames and frozen/silent shares of a raw cut, computed once and cached in its folder.
    Returns None (and drops the clip) when most of it is frozen or silent."""
    raw = Path(row["raw_path"])
    cache = raw.parent / "analysis.json"
    try:
        a = json.loads(cache.read_text())
    except (OSError, ValueError):
        raw_s = _duration(raw, s.ffmpeg)
        context = _context(raw.parent)
        stream = stream_cfg(s, row["stream"], context.get("name", ""))
        words = transcribe(raw, s.ffmpeg, names=(stream.display, context.get("game", "")))
        lo = max(0.0, row["onset"] - row["raw_t0"] - 15)
        hi = min(raw_s, row["end"] - row["raw_t0"] + 15)
        stills = frames(raw, lo, hi, N_FRAMES, raw.parent, s.ffmpeg)
        frozen, silent = quality_check(raw, s.ffmpeg, raw_s)
        a = {"raw_s": raw_s, "words": [[w.start, w.end, w.text] for w in words], "frozen": frozen, "silent": silent,
             "frames": [[lo + (i + 0.5) * (hi - lo) / N_FRAMES, str(p)] for i, p in enumerate(stills)]}
        cache.write_text(json.dumps(a))
    if a["frozen"] > MAX_FROZEN or a["silent"] > MAX_SILENT:
        set_state(db, row["id"], "dropped", error=f"frozen {a['frozen']:.0%}, silent {a['silent']:.0%}")
        log.info("%s: dropped, frozen %.0f%% silent %.0f%%", row["id"], a["frozen"] * 100, a["silent"] * 100)
        return None
    return a


def score_clip(db, s: Settings, row, a: dict, key: str) -> float:
    """The candidate's Jev rank (0..1, see jev.rank), asked once and kept in the scores table."""
    got = get_scores(db, row["id"])
    if got is not None:
        return got["rank"]
    work = Path(row["raw_path"]).parent
    ctx = _context(work)
    cfg = stream_cfg(s, row["stream"], ctx.get("name", ""))
    body = jev.build_request(cfg.display, ctx.get("game", ""), row["mood"], row["onset"] - row["raw_t0"],
                             [Word(*w) for w in a["words"]], ctx.get("chat", []), [(t, Path(p)) for t, p in a["frames"]])
    response = jev.ask(body, key)
    scores = jev.scores(response)
    chat = bool(ctx.get("chat"))
    rank = jev.rank(scores, crowd=chat, weights=_learned(s).get("weights"))
    save_scores(db, row["id"], {"scores": scores, "answers": response.get("answers")}, rank,
                int(response.get("usage", {}).get("input_tokens", 0)), str(response.get("model", "")))
    # a clip without chat is recorded without its crowd answer, which keeps it out of the fit and the bars
    _record(learn.add_candidate, row["id"], row["stream"], ctx.get("game", ""), row["mood"], row["score"],
            scores if chat else {k: v for k, v in scores.items() if k != "crowd"}, time.time())
    log.info("%s: Jev rank %.2f %s", row["id"], rank, {k: round(v, 2) for k, v in scores.items()})
    return rank


def _prepared(db, s: Settings, row) -> dict | None:
    try:
        return prepare(db, s, row)
    except Exception as e:                          # a broken raw cut fails alone; the other candidates go on
        log.exception("%s: preparation failed", row["id"])
        set_state(db, row["id"], "failed", error=f"prepare: {type(e).__name__}: {e}"[:500])
        return None


def _explore_due(s: Settings) -> bool:
    if LEARN is None or not s.learn:
        return False
    try:
        return learn.explore_due(LEARN, time.time())
    except Exception as e:
        _failed("explore_due", e)
        return False


def choose_clips(db, s: Settings, rows) -> list:
    """Candidates in posting order: by Jev rank, those at or above the slot minimum (jev.MIN_RANK unless learned),
    then, when an exploration post is due, those just below it; by detector score only when no Jev key is set. A
    Jev failure posts nothing this slot, since Jev's frame check is the nudity gate. One at a time, in the caller's
    worker."""
    key = SECRETS["jev"]                            # read once at startup: never the Keychain under HEAVY
    EXPLORING.clear()
    ranked, by_jev = [], key is not None
    for row in rows:
        if STOP.is_set():
            break
        a = _prepared(db, s, row)
        if a is None:
            continue
        rank = None
        if by_jev:
            try:
                rank = score_clip(db, s, row, a, key)
            except Exception as e:                  # only Jev looks at the frames for nudity: nothing posts blind
                log.warning("Jev unavailable (%s); this slot waits for it", e)
                return []
        ranked.append((rank, row))
    if not by_jev:
        return [row for _, row in sorted(ranked, key=lambda x: -x[1]["score"])]
    low, _ = _bars(s)
    best_first = sorted(ranked, key=lambda x: -x[0])
    below = [row for rank, row in best_first if low - learn.EXPLORE_BAND <= rank < low][:1]   # one try a slot
    if below and _explore_due(s):
        EXPLORING.add(below[0]["id"])
    return [row for rank, row in best_first if rank >= low] + [row for row in below if row["id"] in EXPLORING]


def _state(db, clip_id: str) -> str:
    return db.execute("select state from clips where id = ?", (clip_id,)).fetchone()[0]


def _slot_work(db, s: Settings, process, now) -> str | None:
    """Candidates are ranked under HEAVY, the channels are signed in outside it (once a pass, only when there is a
    candidate), and each clip is claimed again under HEAVY after a fresh check, so a clip another pass took
    meanwhile is never processed again."""
    with HEAVY:
        t = now()
        last = flag(db, "last_slot") or ""
        if not post_ok(db, s, t) or (last and t - float(last) < s.slot_minutes * 60):
            return None
        ranked = choose_clips(db, s, cut_rows(db, t, 2 * s.slot_minutes * 60))
    clients = None
    for row in ranked:
        if STOP.is_set():
            break
        if clients is None and process is process_clip:
            clients = _sign_ins(db, _postable_profiles(db, s, now()))
        with HEAVY:
            if _state(db, row["id"]) != "cut" or not clip_ok(db, s, row, now()):
                continue
            flag(db, "last_slot", str(now()))
            waits = (process(db, s, row, clients) if clients is not None else process(db, s, row)) == WAITS
            if not waits and _state(db, row["id"]) != "dropped":
                return row["id"]
            flag(db, "last_slot", last)     # a rejected edit, or a clip its channel could not take now, keeps the slot
    return None


def post_ok(db, s: Settings, t: float, profile: str | None = None) -> bool:
    """The one check before posting, for slots, the fast lane and companions alike, called under HEAVY: disk, and
    for the profile (each channel has its own limits) its pause flag, its daily cap from the current upload history,
    and min_gap_minutes since its last upload (or saved clip). Without a profile: whether any channel can post."""
    if profile is None:
        return any(post_ok(db, s, t, p) for p in profiles(s))
    if not disk_ok(s):
        return False
    history = profile if s.variant_profiles else None    # without routing: every upload counts, as it always did
    last = last_upload(db, profile=history)
    if last is not None and t - last < s.min_gap_minutes * 60:
        return False
    if not s.upload:                               # pauses and the daily cap are about YouTube; saving is not
        return True
    return not paused(db, profile) and uploads_since(db, t - 86400, profile=history) < s.max_uploads_per_day


def clip_ok(db, s: Settings, row, t: float) -> bool:
    """post_ok for one candidate: a clip that keeps its variant waits for that variant's channel, so it never takes a
    slot or the fast lane from a channel that can post."""
    kept = row["variant"] in s.caption_variants
    return post_ok(db, s, t, profile_for(s, row["variant"]) if kept else None)


def postable(db, s: Settings, t: float) -> tuple[str, ...]:
    """The presentation variants whose channel can post now, in configured order."""
    return tuple(v for v in s.caption_variants if post_ok(db, s, t, profile_for(s, v)))


async def slot_once(s: Settings, db, *, process=process_clip, now=time.time) -> str | None:
    t = now()
    max_age = 2 * s.slot_minutes * 60
    for raw in expire(db, t, max_age):            # retention runs even while uploads are paused
        _remove(Path(raw))
    last = flag(db, "last_slot")
    if last and t - float(last) < s.slot_minutes * 60:      # a restart cannot upload twice in one slot
        return None
    return await asyncio.to_thread(_slot_work, db, s, process, now)


def prep_once(db, s: Settings, *, process=process_clip, now=time.time) -> str | None:
    """Prepare and score each new cut as soon as it exists (one at a time), and post a fast-lane clip at once:
    rank at or above the fast lane bar (s.fast_lane unless learned), uploads allowed, and min_gap_minutes since the
    previous upload."""
    key = SECRETS["jev"]
    for row in cut_rows(db, now(), 2 * s.slot_minutes * 60):
        if STOP.is_set():
            return None
        with HEAVY:
            row = db.execute("select * from clips where id = ?", (row["id"],)).fetchone()   # fresh, under HEAVY
            if row["state"] != "cut":
                continue
            a = _prepared(db, s, row)
            if a is None or key is None:
                continue
            try:
                rank = score_clip(db, s, row, a, key)
            except Exception as e:
                log.warning("%s: Jev unavailable (%s); it waits for the slot", row["id"], e)
                continue
            if not (rank >= _bars(s)[1] and clip_ok(db, s, row, now())):
                continue
        clients = _sign_ins(db, _postable_profiles(db, s, now())) if process is process_clip else None
        with HEAVY:
            row = db.execute("select * from clips where id = ?", (row["id"],)).fetchone()   # claimed afresh
            if row["state"] != "cut" or rank < _bars(s)[1] or not clip_ok(db, s, row, now()):
                continue                            # rechecked: the learned bar may have moved meanwhile
            log.info("%s: fast lane (rank %.2f)", row["id"], rank)
            waits = (process(db, s, row, clients) if clients is not None else process(db, s, row)) == WAITS
            if not waits and _state(db, row["id"]) != "dropped":   # its channel could not take it: try the next
                return row["id"]
    return None


def _watch_through(profile: str, posts: list[tuple[str, float]], now: float) -> dict[str, float]:
    """Average percentage viewed of (video_id, posted_at) posts, for a profile signed in with the Analytics
    permission; {} when it is not, or on any error, which never costs the views snapshot."""
    if not posts:
        return {}
    day = lambda at: datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d")
    try:
        if upload.has_analytics(profile):
            return upload.watched(profile, [video_id for video_id, _ in posts],
                                  day(min(at for _, at in posts)), day(now))
    except Exception as e:
        log.warning("watch-through of %s not read (%s: %s)", profile, type(e).__name__, e)
    return {}


def learn_once(s: Settings, db, *, now=time.time, client=client, stats=video_stats) -> None:
    """Take the view-count snapshots that are due, then update the learned values. It never signs in through
    _youtube, which pauses a channel: a failed sign-in or request skips that channel until the next pass."""
    if LEARN is None:
        return
    for profile, due in learn.due_snapshots(LEARN, now()).items():
        try:
            if profile not in STATS_CLIENTS:      # building one costs a request of the upload quota
                STATS_CLIENTS[profile] = client(profile)[0]
            got = stats(STATS_CLIENTS[profile], sorted({video_id for video_id, _, _ in due}))
            watch = _watch_through(profile, [(v, p) for v, a, p in due if a == 72 and v in got], now())
            for video_id, age_h, _ in due:
                if video_id in got:
                    learn.add_snapshot(LEARN, video_id, age_h, *got[video_id], now(),
                                       watched=watch.get(video_id) if age_h == 72 else None)
        except Exception as e:
            STATS_CLIENTS.pop(profile, None)      # a stale sign-in is rebuilt on the next pass
            log.warning("view counts of %s not read (%s: %s); the next pass retries", profile, type(e).__name__, e)
    learn.update(LEARN, s.max_uploads_per_day * (len(profiles(s)) if s.variant_profiles else 1), now())


async def _learn_loop(s: Settings, db) -> None:
    while True:
        try:
            await asyncio.to_thread(learn_once, s, db)
        except Exception:
            log.exception("learning pass failed")
        await asyncio.sleep(LEARN_EVERY_S)


async def _prep_loop(s: Settings, db) -> None:
    while True:
        try:
            await asyncio.to_thread(companion_once, db, s)    # already rendered: it goes before any new Short
        except Exception:
            log.exception("landscape companion failed")
        try:
            await asyncio.to_thread(prep_once, db, s)
        except Exception:
            log.exception("preparation failed")
        await asyncio.sleep(PREP_EVERY_S)


async def _instagram_loop(s: Settings, db) -> None:
    while True:
        for profile in instagram_profiles(s):
            try:
                await asyncio.to_thread(instagram.advance, db, profile, s.instagram_account_id, STOP, logger=log)
            except Exception:
                log.exception("Instagram queue check failed for %s", profile)
        await asyncio.sleep(instagram.CHECK_EVERY_S)


def slot_wait(s: Settings, db, now: float) -> float:
    """Seconds until the next slot boundary (last slot start + slot_minutes), at least SLOT_POLL_S."""
    last = flag(db, "last_slot")
    due = float(last) + s.slot_minutes * 60 if last else now
    return max(SLOT_POLL_S, due - now)


def _remove(raw: Path) -> None:
    raw.unlink(missing_ok=True)
    try:
        raw.parent.rmdir()
    except OSError:
        pass


def sweep_clips(s: Settings, db, now: float) -> int:
    """Remove clip directories older than KEEP_CLIPS_S unless their clip is still waiting or in work."""
    root = clips_root(s)
    if root.is_symlink() or not root.is_dir():
        return 0
    root = root.resolve()
    live = {Path(r[0]).parent.resolve() for r in
            db.execute("select raw_path from clips where state in ('cut', 'rendering', 'uploading') union all"
                       " select video_path from outputs where state in ('pending', 'uploading') and created > ?",
                       (now - KEEP_CLIPS_S,))}
    removed = 0
    for d in root.iterdir():
        if d.is_symlink() or not d.is_dir() or d.resolve().parent != root:   # never follow a link out of clips
            continue
        if d.resolve() in live or d.lstat().st_mtime > now - KEEP_CLIPS_S:
            continue
        shutil.rmtree(d)
        removed += 1
    return removed


def check_uploads(db, ids: Sequence[str], profile: str) -> list[str] | None:
    """The profile's rejected or missing videos. Its sign-in is recorded first: one that lands on another profile's
    channel pauses both and looks nothing up (None)."""
    yt, channel = client(profile)
    if not _record_channel(db, profile, channel):
        return None
    return strike_check(yt, ids)


def notify(text: str) -> None:
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"autoclipper\""],
                   check=False, timeout=30)


async def _retry_secrets() -> None:
    """After a failed or timed-out Keychain read (a launchd restart after a reboot, before the login keychain is
    available), read it again: in a thread with the same time limit, never under HEAVY."""
    got = await asyncio.to_thread(_read_secrets)
    if got["ok"]:
        SECRETS.update(got)
        _HELIX.clear()                              # discovery builds its Twitch client from the new credentials
        log.info("Keychain answered: Jev ranking and Twitch discovery are back")


async def guard_once(s: Settings, db, *, check=None, notify=notify) -> None:
    check = check or (lambda ids, profile: check_uploads(db, ids, profile))
    try:
        sweep_clips(s, db, time.time())
        sweep_buffers(s, time.time())
    except Exception:
        log.exception("sweep failed")
    if not SECRETS.get("ok"):
        await _retry_secrets()
    if not s.upload:                               # no YouTube at all with uploads off
        return
    for profile in profiles(s):                    # lift expired upload-limit pauses
        why = paused(db, profile)
        if why.startswith("limit:") and float(why.split(":", 1)[1]) <= time.time():
            lift(db, profile, why)                 # only that limit: a strike written since stays
    for profile in profiles(s):                    # each channel's own uploads, with its own client
        ids = uploaded_ids(db, profile=profile)
        if not ids or paused(db, profile).startswith("strike:"):
            continue
        try:
            bad = await asyncio.to_thread(check, ids, profile)
        except Exception:                          # one channel's failed check does not skip the others
            log.exception("strike check failed for %s", profile)
            continue
        if bad:
            pause(db, profile, "strike:" + ",".join(bad))
            notify(f"Uploads on {profile} paused: {len(bad)} video(s) rejected or removed. "
                   f"Run `autoclipper resume --profile {profile}` after checking.")


async def _slot_loop(s: Settings, db) -> None:
    while True:
        try:
            await slot_once(s, db)
        except Exception:
            log.exception("slot failed")
        await asyncio.sleep(slot_wait(s, db, time.time()))


async def _guard_loop(s: Settings, db) -> None:
    while True:
        try:
            await guard_once(s, db)
        except Exception:
            log.exception("guard failed")
        await asyncio.sleep(GUARD_EVERY_S)


# --- discovery (round 3): which streams hold the MAX_STREAMS slots

@dataclass
class Watch:
    cfg: StreamCfg
    kind: str                       # "pin", "prefer" or "found"
    low: int = 0                    # refreshes in a row below min_viewers
    viewers: int = 0
    busy: bool = False              # an open event or a cut pending: never swapped out for a preferred stream
    task: asyncio.Task | None = None
    state: dict | None = None


def _person(cfg: StreamCfg) -> str:
    return cfg.channel.lstrip("@").lower()


def plan_slots(watched: dict[str, Watch], d: Discover, listing: dict, ok: set[str], live_named: set[str],
               cap: int, *, eligible: dict | None = None, taken: Sequence[StreamCfg] = ()) -> dict[str, Watch]:
    """The streams to watch after a refresh, as Watch entries (kind, low, viewers set; task not started).
    Order of claim: busy watches still live (any kind: never replaced mid-event or before their cut ran); live
    pinned streams; live preferred streams (any game; one person on two platforms takes the first entry);
    discovered streams still live in the game (gone after two refreshes in a row under min_viewers; kept as they
    are when their platform's listing failed); then the biggest new ones that allow clipping (`eligible`).
    `listing` is the raw listing and decides liveness; `taken` are fixed streams, whose people are not picked again."""
    out: dict[str, Watch] = {}
    never = {c.key for c in d.never}
    eligible = listing if eligible is None else eligible
    people = {_person(c) for c in taken}

    def add(cfg: StreamCfg, kind: str, low: int = 0, viewers: int = 0) -> None:
        if cfg.key in never:
            return                              # owner-maintained: never watched, even when pinned or preferred
        if len(out) < cap and cfg.key not in out and _person(cfg) not in people \
                and all(_person(w.cfg) != _person(cfg) for w in out.values()):
            out[cfg.key] = Watch(cfg, kind, low, viewers)

    def still_found(w: Watch) -> tuple[bool, int, int]:
        if w.cfg.platform not in ok:
            return True, w.low, w.viewers
        live = listing.get(w.cfg.key)
        if live is None:
            return False, 0, 0                  # offline or no longer in the game
        low = w.low + 1 if live.viewers < d.min_viewers else 0
        return low < 2, low, live.viewers

    found = sorted((w for w in watched.values() if w.kind == "found"), key=lambda w: -w.viewers)
    for w in watched.values():
        if w.busy:
            keep, low, viewers = still_found(w) if w.kind == "found" else (w.cfg.key in live_named, 0, w.viewers)
            if keep:
                add(w.cfg, w.kind, low, viewers)
    for cfg in d.pin:
        if cfg.key in live_named:
            add(cfg, "pin")
    for cfg in d.prefer:
        if cfg.key in live_named:
            add(cfg, "prefer")
    for w in found:
        keep, low, viewers = still_found(w)
        if keep:
            add(w.cfg, "found", low, viewers)
    for live in sorted(eligible.values(), key=lambda x: -x.viewers):
        if live.viewers >= d.min_viewers:
            add(StreamCfg(live.platform, live.channel, name=live.name if not any(c in live.name for c in "@<>") else ""),
                "found", 0, live.viewers)
    return out


def fixed_streams(s: Settings) -> list[StreamCfg]:
    """The [[stream]] entries to run: not on the never list, and one person only once (first entry wins)."""
    never = {c.key for c in s.discover.never} if s.discover else set()
    out = []
    for cfg in s.streams:
        if cfg.key in never:
            log.info("fixed stream %s is on the never list; not started", cfg.key)
        elif any(_person(c) == _person(cfg) for c in out):
            log.info("fixed stream %s is the same person as one already watched; not started", cfg.key)
        else:
            out.append(cfg)
    return out


_HELIX: list = []                   # the Helix client once credentials were found; [None] when they are missing
_CLIPS_OK: dict[str, tuple[bool, float]] = {}   # per channel: allows clipping?, when it was checked
CLIP_CACHE_S = 86400
CLIP_FAIL_CACHE_S = 600            # a failed check is "unknown" for 10 minutes, then asked again
CLIP_CHECKS_PER_REFRESH = 20


def _recent_clip(live) -> bool:
    if live.platform == "twitch":
        return _HELIX[0].recent_clip(live.id)
    return disc.kick_recent_clip(live.channel)


def allowed(listing: dict, d: Discover, *, check=_recent_clip, now=time.time) -> dict:
    """The streams that may be picked as new: big enough, not on the never list, and allowing clipping (a clip in
    the last 7 days; checked biggest first, at most CLIP_CHECKS_PER_REFRESH a refresh, a yes/no kept 24 h, a failed
    check kept as unknown for 10 minutes). Liveness of watched streams comes from the raw listing, not from this."""
    never = {c.key for c in d.never}
    out, checks = {}, 0
    for key, live in sorted(listing.items(), key=lambda kv: -kv[1].viewers):
        if key in never:
            log.info("discovery: skip %s (never list)", key)
            continue
        if live.viewers < d.min_viewers:
            continue
        ok, when = _CLIPS_OK.get(key, (None, 0.0))
        age = now() - when
        if ok == "unknown" and age < CLIP_FAIL_CACHE_S:
            continue
        if ok is None or ok == "unknown" or age > CLIP_CACHE_S:
            if checks >= CLIP_CHECKS_PER_REFRESH:
                continue                        # its turn comes at a later refresh
            checks += 1
            try:
                ok = check(live)
            except Exception as e:
                _CLIPS_OK[key] = ("unknown", now())
                log.info("discovery: skip %s for now (clip check failed: %s)", key, e)
                continue
            _CLIPS_OK[key] = (ok, now())
        if ok:
            out[key] = live
        else:
            log.info("discovery: skip %s (no clip in the last 7 days: clips off or restricted)", key)
    return out


def _helix():
    """The Twitch Helix client, made once from the credentials read at startup; None without them."""
    if not _HELIX:
        creds = SECRETS["twitch"]
        _HELIX.append(disc.Helix(*creds) if creds else None)
        if creds is None:
            log.warning("no Twitch client id/secret in the Keychain: Twitch discovery is skipped and Twitch "
                        "categories come from streamlink")
    return _HELIX[0]


def game_name(platform: str, category: str | None) -> str | None:
    """A platform's live category as a game name: None for YouTube's broad categories ("Gaming", "People & Blogs")."""
    if not category or (platform == "youtube" and category in YOUTUBE_CATEGORIES):
        return None
    return category


def fixed_game(cfg: StreamCfg) -> str | None:
    """The game a [[stream]] entry is live in now (None when offline or unknown): Twitch from Helix, Kick from
    its channel API, YouTube (and Twitch without Helix credentials) from streamlink's metadata."""
    if cfg.platform == "twitch" and (helix := _helix()) is not None:
        return game_name("twitch", helix.category(cfg.channel))
    if cfg.platform == "kick":
        return game_name("kick", disc.kick_category(cfg.channel))
    return game_name(cfg.platform, disc.live_category(cfg.url))   # a failed probe raises: the name is kept


async def _game_loop(fixed: Sequence[StreamCfg], states: dict[str, dict], *, sleep=asyncio.sleep,
                     clock=time.monotonic) -> None:
    """Keep each fixed stream's game current in the state its cuts write to context.json, like discovered streams,
    on 5-minute deadlines however long the lookups take. A lookup with no game clears it; a failed one keeps it
    until GAME_STALE_S has passed since the last lookup that answered."""
    due, answered = clock(), {cfg.key: clock() for cfg in fixed}
    while True:
        for cfg in fixed:
            state = states[cfg.key]
            try:
                game = await asyncio.to_thread(fixed_game, cfg) or ""
            except Exception as e:
                log.warning("%s: category not read (%s)", cfg.key, e)
                if state.get("game") and clock() - answered[cfg.key] >= GAME_STALE_S:
                    log.info("%s: game %s no longer confirmed; dropped", cfg.key, state["game"])
                    state["game"] = ""
                continue
            answered[cfg.key] = clock()
            if game != state.get("game", ""):
                log.info("%s: live in %s", cfg.key, game or "no known game")
                state["game"] = game
        due = max(due + GAME_EVERY_S, clock())
        await sleep(due - clock())


def list_live(d: Discover) -> tuple[dict, set[str]]:
    """Live streams in the game per platform, keyed like StreamCfg.key, and the platforms whose listing worked."""
    listing, ok = {}, set()
    for platform in d.platforms:
        try:
            if platform == "twitch":
                if _helix() is None:
                    continue
                game_id = _HELIX[0].game_id(d.game)
                lives = _HELIX[0].streams(game_id, d.language) if game_id else []
            else:
                lives = disc.kick_streams(d.game, d.language)
            ok.add(platform)
            listing.update({f"{x.platform}:{x.channel}": x for x in lives})
        except Exception as e:
            log.warning("discovery: %s listing failed (%s); its current streams are kept", platform, e)
    return listing, ok


async def discover_loop(s: Settings, db, *, lister=list_live, allow=allowed, probe=disc.probe, run_stream=None,
                        sleep=asyncio.sleep) -> None:
    """Every refresh: which streams to watch (plan_slots), then stop the ones that left and start the new ones."""
    d = s.discover
    run_stream = run_stream or (lambda cfg, state: stream_task(cfg, s, db, state=state))
    fixed = fixed_streams(s)
    watched: dict[str, Watch] = {}
    try:
        while True:
            try:
                listing, ok = await asyncio.to_thread(lister, d)
                eligible = await asyncio.to_thread(allow, listing, d)
                named = [*d.pin, *d.prefer]
                probed = await asyncio.gather(*(asyncio.to_thread(probe, c.url) for c in named))
                live_named = {c.key: game_name(c.platform, cat) for c, (live, cat) in zip(named, probed) if live}
                for w in watched.values():
                    det = w.state["det"]
                    w.busy = (det is not None and det.onset is not None) or w.state["pending"] > 0
                plan = plan_slots(watched, d, listing, ok, set(live_named), MAX_STREAMS - len(fixed),
                                  eligible=eligible, taken=fixed)
                retired = [watched.pop(k) for k in [k for k in watched if k not in plan]]
                for w in retired:
                    w.task.cancel()                 # capture and chat stop by handle; pending cuts still finish
                    log.info("discovery: stop %s", w.cfg.key)
                await asyncio.gather(*(w.task for w in retired), return_exceptions=True)   # five means five
                for key, w in plan.items():
                    game = listing[key].game if key in listing else live_named.get(key) or ""
                    if key in watched:
                        watched[key].kind, watched[key].low, watched[key].viewers = w.kind, w.low, w.viewers
                        watched[key].state["game"] = game
                        continue
                    w.state = {"game": game, "det": None, "pending": 0}
                    w.task = asyncio.create_task(run_stream(w.cfg, w.state))
                    watched[key] = w
                    log.info("discovery: watch %s (%s, %s viewers, %s)", key, w.kind, w.viewers, game or "?")
            except Exception:
                log.exception("discovery refresh failed")
            await sleep(d.refresh_minutes * 60)
    finally:
        for w in watched.values():
            w.task.cancel()
        await asyncio.gather(*(w.task for w in watched.values()), return_exceptions=True)


LOG_BYTES, LOG_FILES = 20_000_000, 5
SECRET_TIMEOUT_S = 20


def _read_secrets() -> dict:
    """The Jev key and Twitch credentials. A Keychain that fails or does not answer within SECRET_TIMEOUT_S leaves
    Jev and Twitch discovery off ("ok" false) until a later guard tick reads it successfully."""
    got: dict = {}

    def read() -> None:
        try:
            got["jev"], got["twitch"] = jev.api_key(), disc.twitch_credentials()
        except Exception as e:
            got["error"] = e

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(SECRET_TIMEOUT_S)
    if reader.is_alive() or "error" in got:
        why = f"did not answer in {SECRET_TIMEOUT_S} s" if reader.is_alive() else f"failed ({got['error']})"
        log.warning("Keychain %s: Jev ranking and Twitch discovery are off until it answers", why)
        return {"jev": None, "twitch": None, "ok": False}
    if not got["jev"]:
        log.warning("no Jev key in the Keychain: ranking by detector score for this run")
    return {"jev": got["jev"], "twitch": got["twitch"], "ok": True}


def _log_to(s: Settings) -> list[logging.Handler]:
    """The daemon's log: a rotating file on the data volume, plus the terminal when run by hand."""
    (s.data_dir / "logs").mkdir(exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(s.data_dir / "logs" / "autoclipper.log",
                                                     maxBytes=LOG_BYTES, backupCount=LOG_FILES)]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    return handlers


async def main(config: Path) -> None:
    global LEARN
    s = load(config)
    lock_path = s.data_dir / "autoclipper.lock"
    lock = open(lock_path, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit(f"autoclipper is already running (another process holds {lock_path})") from None
    awake, handlers = None, _log_to(s)
    try:
        db = open_db(s.data_dir / "autoclipper.db")
        try:
            LEARN = learn.open_learning(learn_path(s))
        except Exception as e:                     # the daemon runs exactly as before, on the hand-set values
            log.warning("learning record unavailable (%s: %s); hand-set values apply", type(e).__name__, e)
            LEARN = None
        log.info("recovered %d interrupted clips", recover(db))
        migrate_pause(db, profiles(s), s.youtube_profile)   # the old global pause, once, where it belongs
        if not s.variant_profiles:                 # a single-profile config: remember which channel it posts on
            record_posting_profile(db, s.youtube_profile)
        SECRETS.update(await asyncio.to_thread(_read_secrets))     # once, before any task, never on the loop
        awake = subprocess.Popen(["/usr/bin/caffeinate", "-is", "-w", str(os.getpid())])   # keeps the Mac awake
        fixed = fixed_streams(s)
        states: dict[str, dict] = {c.key: {} for c in fixed}       # shared with _game_loop, which sets "game"
        tasks = [asyncio.create_task(stream_task(c, s, db, state=states[c.key])) for c in fixed]
        if fixed:
            tasks.append(asyncio.create_task(_game_loop(fixed, states)))
        if s.discover is not None:
            tasks.append(asyncio.create_task(discover_loop(s, db)))
        tasks += [asyncio.create_task(_slot_loop(s, db)), asyncio.create_task(_guard_loop(s, db)),
                  asyncio.create_task(_prep_loop(s, db)), asyncio.create_task(_learn_loop(s, db)),
                  asyncio.create_task(asyncio.to_thread(check_channels, db, s))]   # beside posting, not before it
        if s.instagram_account_id and s.upload and s.privacy == "public":
            tasks.append(asyncio.create_task(_instagram_loop(s, db)))

        def stop() -> None:
            STOP.set()
            for t in tasks:
                t.cancel()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop)
        try:
            await asyncio.gather(*tasks)           # a crash ends the process; launchd starts it again
        except asyncio.CancelledError:
            if not STOP.is_set():
                raise
            log.info("stopped by signal")              # a clean stop exits quietly, without a traceback
        finally:
            for t in [*tasks, *CUT_TASKS]:
                t.cancel()
            await asyncio.gather(*tasks, *CUT_TASKS, return_exceptions=True)
            # A cancelled task does not stop its worker thread: an upload, companion or Reel may still be posting.
            # The lock is held until every worker returns (each stops at its next STOP checkpoint), so a new
            # instance can never post beside one that is finishing.
            STOP.set()
            await asyncio.get_running_loop().shutdown_default_executor()
    finally:
        if LEARN is not None:
            LEARN.close()
            LEARN = None
        if awake is not None:
            awake.terminate()
            awake.wait(5)
        for h in handlers:
            log.removeHandler(h)
            h.close()
        log.propagate = True
        lock.close()
