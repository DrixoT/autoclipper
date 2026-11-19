import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Sequence

from .config import DEFAULT_PROFILE

STATES = ("cut", "rendering", "uploading", "uploaded", "saved", "dropped", "failed")   # saved: rendered, upload off
_FIELDS = {"video_id", "uploaded_at", "error", "title", "provider", "raw_path", "youtube_profile", "variant",
           "privacy"}

_SCHEMA = f"""
create table if not exists clips (
    id text primary key, stream text not null, onset real not null, "end" real not null,
    score real not null, mood text not null, sample text not null, raw_path text not null,
    raw_t0 real not null, state text not null, video_id text, uploaded_at real,
    created real not null, updated real not null, error text, title text, provider text,
    youtube_profile text not null default '{DEFAULT_PROFILE}', variant text, privacy text);
create table if not exists kv (key text primary key, value text);
create table if not exists scores (clip_id text primary key, answers text not null, rank real not null,
    input_tokens integer, model text, created real not null);
create table if not exists outputs (clip_id text not null, format text not null, state text not null,
    meta text not null, video_path text not null, thumb_path text not null, youtube_profile text not null,
    video_id text, uploaded_at real, error text, created real not null, updated real not null,
    primary key (clip_id, format));
create table if not exists llm_usage (day text, provider text, calls integer not null,
    primary key (day, provider));
"""


class _Rows:
    """A statement's result, read in full while the connection lock is held."""

    def __init__(self, cursor: sqlite3.Cursor):
        self._rows = cursor.fetchall()
        self.rowcount = cursor.rowcount
        self._next = 0

    def fetchone(self):
        if self._next >= len(self._rows):
            return None
        self._next += 1
        return self._rows[self._next - 1]

    def fetchall(self):
        rows, self._next = self._rows[self._next:], len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class _Shared(sqlite3.Connection):
    """One connection used by the event loop and every worker thread. sqlite3 does not allow two threads inside
    the same connection at once ("bad parameter or other API misuse"), so each statement runs, and its rows are
    read, under one lock; a `with db:` transaction holds the lock until it commits."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()

    def execute(self, sql, parameters=()):
        with self._lock:
            return _Rows(super().execute(sql, parameters))

    def executescript(self, script):
        with self._lock:
            return super().executescript(script)

    def __enter__(self):
        self._lock.acquire()
        return super().__enter__()

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self._lock.release()


def open_db(path: Path) -> sqlite3.Connection:
    # Autocommit, and shared with the worker thread; the parent dir is never created here,
    # so an unmounted volume fails loudly instead of writing to the internal disk.
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, factory=_Shared)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.executescript(_SCHEMA)
    db.execute("begin immediate")
    with db:
        columns = {column["name"] for column in db.execute("pragma table_info(clips)")}
        if "youtube_profile" not in columns:
            db.execute("alter table clips add column youtube_profile text not null "
                       f"default '{DEFAULT_PROFILE}'")
        if "variant" not in columns:
            db.execute("alter table clips add column variant text")
        if "privacy" not in columns:              # the Short's privacy when it was uploaded; null before it was kept
            db.execute("alter table clips add column privacy text")
    return db


def add_clip(db, stream: str, onset: float, end: float, score: float, mood: str,
             sample: Sequence[str], raw_path: str, raw_t0: float) -> str:
    cid, now = uuid.uuid4().hex[:12], time.time()
    db.execute('insert into clips (id, stream, onset, "end", score, mood, sample, raw_path, raw_t0,'
               ' state, created, updated) values (?,?,?,?,?,?,?,?,?,?,?,?)',
               (cid, stream, onset, end, score, mood, json.dumps(list(sample)), raw_path, raw_t0,
                "cut", now, now))
    return cid


def set_state(db, clip_id: str, state: str, **fields) -> None:
    if state not in STATES or not fields.keys() <= _FIELDS:
        raise ValueError(f"bad state or field: {state} {sorted(fields)}")
    cols = "".join(f", {k} = ?" for k in fields)
    db.execute(f"update clips set state = ?, updated = ?{cols} where id = ?",
               (state, time.time(), *fields.values(), clip_id))


def pick_variant(db, variants: Sequence[str], routes: dict[str, str] | None = None) -> str | None:
    """The presentation variant with the fewest posted (uploaded or saved) clips, ties in list order, so failed
    or interrupted clips do not skew the split. With routes (variant -> profile) only the posts of each variant on
    its own profile count. None when no variant is given (no channel can post)."""
    counts = {(v, p): n for v, p, n in db.execute(
        "select variant, youtube_profile, count(*) from clips where state in ('uploaded', 'saved')"
        " group by variant, youtube_profile")}
    def posted(v: str) -> int:
        return sum(n for (variant, p), n in counts.items() if variant == v and (routes is None or p == routes[v]))
    return min(variants, key=posted, default=None)


def expire(db, now: float, max_age_s: float) -> list[str]:
    rows = db.execute("""update clips set state = 'dropped', updated = ?
                         where state = 'cut' and created < ? returning raw_path""",
                      (time.time(), now - max_age_s)).fetchall()
    return [r["raw_path"] for r in rows]


def recover(db) -> int:
    n = db.execute("update clips set state = 'failed', error = 'interrupted during upload'"
                   " where state = 'uploading'").rowcount
    n += db.execute("update outputs set state = 'failed', error = 'interrupted during upload'"
                    " where state = 'uploading'").rowcount
    return n + db.execute("update clips set state = 'cut' where state = 'rendering'").rowcount


# The YouTube cap, gap and strike guard count both Shorts and landscape videos, per channel (profile).
_POSTED = ("select video_id, uploaded_at, youtube_profile, state from clips union all"
           " select video_id, uploaded_at, youtube_profile, state from outputs where format = 'landscape'")
_ON = "(? is null or youtube_profile = ?)"            # one profile, or every profile when None


def uploads_since(db, since: float, *, profile: str | None = None) -> int:
    return db.execute(f"select count(*) from ({_POSTED}) where state = 'uploaded' and uploaded_at >= ? and {_ON}",
                      (since, profile, profile)).fetchone()[0]


def uploaded_ids(db, limit: int = 50, *, profile: str = DEFAULT_PROFILE) -> list[str]:
    rows = db.execute(f"select video_id from ({_POSTED}) where state = 'uploaded' and youtube_profile = ?"
                      " order by uploaded_at desc limit ?", (profile, limit))
    return [r[0] for r in rows]


def flag(db, key: str, value: str | None = None) -> str | None:
    if value is not None:
        db.execute("insert into kv (key, value) values (?, ?)"
                   " on conflict (key) do update set value = excluded.value", (key, value))
        return value
    row = db.execute("select value from kv where key = ?", (key,)).fetchone()
    return row[0] if row else None


PAUSED = "uploads_paused"      # + ":<profile>"; the old global key is only read once, by migrate_pause


def paused(db, profile: str) -> str:
    """Why uploads on this profile are paused ("auth", "limit:<until>", "strike:<ids>"), or "" when they are not."""
    return flag(db, f"{PAUSED}:{profile}") or ""


POSTING_PROFILE = "posting_profile"   # the youtube_profile of the last single-profile (unrouted) daemon start


def record_posting_profile(db, profile: str) -> None:
    flag(db, POSTING_PROFILE, profile)


def posting_profile(db) -> str | None:
    return flag(db, POSTING_PROFILE) or None


def migrate_pause(db, profiles: Sequence[str], fallback: str) -> None:
    """Move the old global pause into the profiles' own pauses, in one transaction, and clear it. A strike goes to
    every given profile. Anything else (an upload limit, a failed sign-in) belonged to the one channel the old
    single-profile config posted on: the recorded posting profile when it is known, else the profile of the most
    recent upload, else `fallback` (youtube_profile). A strike always wins over a temporary pause; another pause a
    profile has is kept."""
    with db:                                       # holds the connection lock for the whole transaction
        db.execute("begin immediate")
        legacy = flag(db, PAUSED)
        if not legacy:
            return
        if not legacy.startswith("strike:"):
            last = db.execute(f"select youtube_profile from ({_POSTED}) where state = 'uploaded'"
                              " and uploaded_at is not null order by uploaded_at desc limit 1").fetchone()
            profiles = (posting_profile(db) or (last[0] if last else fallback),)
        for profile in profiles:
            own = paused(db, profile)
            if not own or (legacy.startswith("strike:") and not own.startswith("strike:")):
                pause(db, profile, legacy)
        flag(db, PAUSED, "")


def pause(db, profile: str, why: str) -> bool:
    """Pause one profile, atomically: an automatic pause ("auth", "limit:", "duplicate:") never replaces a strike,
    which only a newer strike or a resume ("") does. True when the stored value changed."""
    return db.execute(
        "insert into kv (key, value) values (?, ?) on conflict (key) do update set value = excluded.value"
        " where kv.value is not excluded.value"
        " and (excluded.value = '' or excluded.value like 'strike:%' or kv.value not like 'strike:%')",
        (f"{PAUSED}:{profile}", why)).rowcount == 1


def lift(db, profile: str, expected: str) -> bool:
    """Clear a profile's pause only if it is still exactly the value the caller read (an expired limit)."""
    return db.execute("update kv set value = '' where key = ? and value = ?",
                      (f"{PAUSED}:{profile}", expected)).rowcount == 1


def bump_llm(db, provider: str, day: str) -> int:
    return db.execute("insert into llm_usage (day, provider, calls) values (?, ?, 1)"
                      " on conflict (day, provider) do update set calls = calls + 1 returning calls",
                      (day, provider)).fetchone()[0]


def llm_calls(db, provider: str, day: str) -> int:
    row = db.execute("select calls from llm_usage where day = ? and provider = ?", (day, provider)).fetchone()
    return row[0] if row else 0


def cut_rows(db, now: float, max_age_s: float) -> list[sqlite3.Row]:
    """Clips waiting to be posted, queued less than max_age_s ago, highest detector score first. Age counts
    from queueing, not from the moment, so a clip cut from a recording gets the same wait as a live one."""
    return db.execute("""select * from clips where state = 'cut' and created >= ? order by score desc""",
                      (now - max_age_s,)).fetchall()


def save_scores(db, clip_id: str, answers: dict, rank: float, input_tokens: int, model: str) -> None:
    """A candidate's Jev answers, kept so the weights can be fitted against view counts later."""
    db.execute("insert or replace into scores (clip_id, answers, rank, input_tokens, model, created)"
               " values (?, ?, ?, ?, ?, ?)", (clip_id, json.dumps(answers), rank, input_tokens, model, time.time()))


def get_scores(db, clip_id: str) -> sqlite3.Row | None:
    return db.execute("select * from scores where clip_id = ?", (clip_id,)).fetchone()


def last_upload(db, *, profile: str | None = None) -> float | None:
    """When the last video was posted, or saved with uploads off: the fast lane's spacing counts both."""
    return db.execute(f"select max(uploaded_at) from ({_POSTED}) where state in ('uploaded', 'saved') and {_ON}",
                      (profile, profile)).fetchone()[0]


OUTPUT_STATES = ("pending", "uploading", "uploaded", "saved", "failed", "dropped")
_OUTPUT_FIELDS = {"video_id", "uploaded_at", "error"}


def put_output(db, clip_id: str, fmt: str, state: str, meta: dict, video_path: str, thumb_path: str,
               profile: str, **fields) -> bool:
    """Record a rendered companion. A row that is uploading or uploaded is never replaced, so a clip processed
    again cannot post its companion twice. True when written."""
    if state not in OUTPUT_STATES or not fields.keys() <= _OUTPUT_FIELDS:
        raise ValueError(f"bad output state or field: {state} {sorted(fields)}")
    now = time.time()
    row = {"video_id": None, "uploaded_at": None, "error": None, **fields}
    return db.execute(
        "insert into outputs (clip_id, format, state, meta, video_path, thumb_path, youtube_profile, video_id,"
        " uploaded_at, error, created, updated) values (?,?,?,?,?,?,?,?,?,?,?,?)"
        " on conflict (clip_id, format) do update set state = excluded.state, meta = excluded.meta,"
        " video_path = excluded.video_path, thumb_path = excluded.thumb_path,"
        " youtube_profile = excluded.youtube_profile, video_id = excluded.video_id,"
        " uploaded_at = excluded.uploaded_at, error = excluded.error, updated = excluded.updated"
        " where outputs.state not in ('uploading', 'uploaded')",
        (clip_id, fmt, state, json.dumps(meta), video_path, thumb_path, profile, row["video_id"],
         row["uploaded_at"], row["error"], now, now)).rowcount == 1


def set_output(db, clip_id: str, fmt: str, state: str, **fields) -> None:
    if state not in OUTPUT_STATES or not fields.keys() <= _OUTPUT_FIELDS | {"meta"}:
        raise ValueError(f"bad output state or field: {state} {sorted(fields)}")
    cols = "".join(f", {k} = ?" for k in fields)
    db.execute(f"update outputs set state = ?, updated = ?{cols} where clip_id = ? and format = ?",
               (state, time.time(), *fields.values(), clip_id, fmt))


def get_output(db, clip_id: str, fmt: str) -> sqlite3.Row | None:
    return db.execute("select * from outputs where clip_id = ? and format = ?", (clip_id, fmt)).fetchone()


def next_output(db, profile: str, since: float, *, fmt: str = "landscape", account_id: str | None = None,
                public_only: bool = False) -> sqlite3.Row | None:
    """Find the oldest pending output for the configured destination whose Short is uploaded (and, with
    public_only, was uploaded public)."""
    db.execute("update outputs set state = 'dropped', updated = ? where state = 'pending' and created < ?",
               (time.time(), since))
    return db.execute("select o.* from outputs o join clips c on c.id = o.clip_id where o.state = 'pending'"
                      " and c.state = 'uploaded' and c.youtube_profile = o.youtube_profile"
                      " and o.youtube_profile = ? and o.format = ?"
                      " and (? is null or json_extract(o.meta, '$.account_id') = ?)"
                      " and (not ? or c.privacy = 'public')"
                      " order by o.created limit 1",
                      (profile, fmt, account_id, account_id, public_only)).fetchone()


def drop_pending(db, clip_id: str, fmt: str) -> None:
    db.execute("update outputs set state = 'dropped', updated = ? where clip_id = ? and format = ? and state = 'pending'",
               (time.time(), clip_id, fmt))
