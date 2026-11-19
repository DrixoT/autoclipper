"""The learning record: every scored candidate, editor verdict and public post, with the results that follow."""
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Sequence

import numpy as np

from . import jev
from .store import _Shared, flag

AGES_H = (1, 6, 24, 72)                           # hours after posting at which views and likes are recorded
SNAPSHOT_LATE_S = 86400                           # an age missed by more than this is skipped, not taken late
RESULT_AGE_H = 24                                 # the snapshot a post's result is read from
RESULT_LATE_S = 36 * 3600                         # a 24 h snapshot taken later than this after posting is not a result
GROUP_MIN = 5                                     # results a stream or channel needs before posts are ranked within it
GROUP_DAYS = 14                                   # only posts this recent are compared
Y_DROPPED, Y_PENDING, Y_BASE, Y_SPAN = 0.0, 0.7, 0.4, 0.6   # label: dropped; kept, no result; posted
FIT_MIN = 30                                      # labelled candidates needed before the weights are fitted
FIT_MIN_EACH = 8                                  # ...and this many with y = 0 and with y > 0
FIT_EVERY_S = 86400                               # at most one weight fit a day
F_MIN = 3.5                                       # F statistic the free fit must reach to beat the current weights
WEIGHT_RANGE = (0.05, 0.40)                       # no weight leaves this range
WEIGHT_STEP = 0.03                                # the most a weight moves in one fit
BAR_MIN_CANDIDATES = 30                           # scored candidates in the window needed to move the bars
BAR_WINDOW_S = 86400                              # the bars look at the last day
EDITOR_BUDGET = 80                                # editor calls a day the bars aim for, under the 100 allowed
KEEP_FLOOR = 0.2                                  # lowest share of editor-seen candidates assumed kept
KEEP_DEFAULT = 0.5                                # that share with fewer than 5 seen
SLOT_BAR = (0.35, 0.55)                           # range of the slot minimum rank
FAST_GAP = 0.05                                   # post-at-once is at least this far above the slot minimum
FAST_MAX = 0.80                                   # highest post-at-once rank
EXPLORE_BAND = 0.10                               # exploration admits ranks this far below the slot minimum
EXPLORE_EVERY = 9                                 # normal public posts between exploration posts
EXPLORE_WAIT_S = 3600                             # at least this long between exploration attempts
WIN_P = 0.8                                       # a post at or above this percentile is a winner
WIN_MIN_RESULTS = 10                              # posts with results needed before examples are learned
WIN_MIN = 2                                       # winners needed
WIN_TOP = 3                                       # examples handed to the editor
FIELD_MAX = 200                                   # characters kept of each example field
BAR_LOG_STEP = 0.01                               # a bar that moves this much is logged

_SCHEMA = """
create table if not exists candidates (
    clip_id text primary key, stream text not null, game text not null, mood text not null,
    det_score real not null, scores text not null, created real not null,
    kept integer, explore integer not null default 0,
    profile text, variant text, video_id text, title text, what text, context text, reaction text,
    length_s real, posted_at real);
create index if not exists candidates_video on candidates (video_id);
create table if not exists snapshots (
    video_id text not null, age_h integer not null, views integer not null, likes integer not null,
    watched real, taken real not null, primary key (video_id, age_h));
create table if not exists changes (at real not null, what text not null, before text, after text, why text);
create table if not exists kv (key text primary key, value text);
"""


def open_learning(path: Path) -> sqlite3.Connection:
    # Same settings as store.open_db; the parent dir is never created, so an unmounted volume fails loudly.
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, factory=_Shared)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.executescript(_SCHEMA)
    return db


def add_candidate(ldb, clip_id: str, stream: str, game: str, mood: str, det_score: float,
                  scores: dict[str, float], created: float) -> None:
    ldb.execute("insert or ignore into candidates (clip_id, stream, game, mood, det_score, scores, created)"
                " values (?,?,?,?,?,?,?)", (clip_id, stream, game, mood, det_score, json.dumps(scores), created))


def set_verdict(ldb, clip_id: str, kept: bool) -> None:
    ldb.execute("update candidates set kept = ? where clip_id = ?", (int(kept), clip_id))


def set_posted(ldb, clip_id: str, *, profile: str, variant: str, video_id: str, title: str, what: str,
               context: str, reaction: str, length_s: float, posted_at: float) -> None:
    ldb.execute("update candidates set kept = 1, profile = ?, variant = ?, video_id = ?, title = ?, what = ?,"
                " context = ?, reaction = ?, length_s = ?, posted_at = ? where clip_id = ?",
                (profile, variant, video_id, title, what, context, reaction, length_s, posted_at, clip_id))


def mark_explore(ldb, clip_id: str, now: float) -> None:
    ldb.execute("update candidates set explore = 1 where clip_id = ?", (clip_id,))
    flag(ldb, "explore_at", str(now))


def due_snapshots(ldb, now: float) -> dict[str, list[tuple[str, int, float]]]:
    """Per profile, the (video_id, age_h, posted_at) of every post and age that has been reached with no row yet."""
    due: dict[str, list[tuple[str, int, float]]] = {}
    for r in ldb.execute("select profile, video_id, posted_at from candidates where video_id is not null"
                         " and posted_at >= ? order by posted_at, video_id",
                         (now - max(AGES_H) * 3600 - SNAPSHOT_LATE_S,)):
        have = {a[0] for a in ldb.execute("select age_h from snapshots where video_id = ?", (r["video_id"],))}
        for age in AGES_H:
            if age not in have and 0 <= now - r["posted_at"] - age * 3600 <= SNAPSHOT_LATE_S:
                due.setdefault(r["profile"], []).append((r["video_id"], age, r["posted_at"]))
    return due


def add_snapshot(ldb, video_id: str, age_h: int, views: int, likes: int, taken: float,
                 watched: float | None = None) -> None:
    ldb.execute("insert or replace into snapshots (video_id, age_h, views, likes, watched, taken)"
                " values (?,?,?,?,?,?)", (video_id, age_h, views, likes, watched, taken))


def import_run(ldb, run_db: Path) -> int:
    """Copy an older run's scored clips into the record (existing rows are left alone); returns the rows added."""
    src = sqlite3.connect(f"{Path(run_db).resolve().as_uri()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute("select c.*, s.answers from clips c join scores s on s.clip_id = c.id").fetchall()
    finally:
        src.close()
    added = 0
    for r in rows:
        privacy = r["privacy"] if "privacy" in r.keys() else None     # a run from before that column existed
        posted = r["state"] == "uploaded" and r["video_id"] and privacy in (None, "public")
        dropped = r["state"] == "dropped" and (r["error"] or "").startswith("editor:")
        added += ldb.execute(
            "insert or ignore into candidates (clip_id, stream, game, mood, det_score, scores, created, kept,"
            " profile, variant, video_id, title, what, context, reaction, length_s, posted_at)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r["stream"], "", r["mood"], r["score"], json.dumps(json.loads(r["answers"])["scores"]),
             r["created"], 1 if posted else 0 if dropped else None,
             *((r["youtube_profile"], r["variant"], r["video_id"], r["title"], "", "", "", 0, r["uploaded_at"])
               if posted else (None,) * 9))).rowcount
    return added


def _positions(values: Sequence[float]) -> list[float]:
    """(position - 0.5) / n of each value, ascending; ties share their mean position."""
    n = len(values)
    return [(sum(w < v for w in values) + (sum(w == v for w in values) + 1) / 2 - 0.5) / n for v in values]


def percentiles(ldb, now: float) -> dict[str, float]:
    """video_id -> p: where a post's 24 h views stand among its stream's (or channel's) recent posts."""
    rows = ldb.execute(
        "select c.video_id, c.profile, c.stream, s.views, (select w.watched from snapshots w where"
        " w.video_id = c.video_id and w.watched is not null order by w.age_h desc limit 1) as watched"
        " from candidates c join snapshots s on s.video_id = c.video_id and s.age_h = ?"
        " where c.posted_at >= ? and s.taken - c.posted_at <= ?",
        (RESULT_AGE_H, now - GROUP_DAYS * 86400, RESULT_LATE_S)).fetchall()
    pairs, channels = Counter((r["profile"], r["stream"]) for r in rows), Counter(r["profile"] for r in rows)
    groups: dict[tuple, list] = {}
    for r in rows:
        key = ((r["profile"], r["stream"]) if pairs[r["profile"], r["stream"]] >= GROUP_MIN
               else (r["profile"],) if channels[r["profile"]] >= GROUP_MIN else None)
        if key:
            groups.setdefault(key, []).append(r)
    out = {}
    for group in groups.values():
        views = _positions([r["views"] for r in group])
        seen = [r for r in group if r["watched"] is not None]
        watched = dict(zip((r["video_id"] for r in seen), _positions([r["watched"] for r in seen]))) \
            if len(seen) >= GROUP_MIN else {}
        for r, p in zip(group, views):
            out[r["video_id"]] = (p + watched[r["video_id"]]) / 2 if r["video_id"] in watched else p
    return out


def _complete(scores: dict) -> bool:
    """Scored on every question the rank uses today; a record from before a question existed is not comparable."""
    return all(k in scores for k in (*jev.WEIGHTS, "dead"))


def labels(ldb, now: float) -> list[tuple[dict[str, float], float]]:
    """(scores, y) for every candidate the editor saw; derived on each call, so later results change it."""
    p = percentiles(ldb, now)
    rows = [(json.loads(r["scores"]),
             Y_DROPPED if not r["kept"] else Y_BASE + Y_SPAN * p[r["video_id"]] if r["video_id"] in p else Y_PENDING)
            for r in ldb.execute("select scores, kept, video_id from candidates where kept is not null")]
    return [(s, y) for s, y in rows if _complete(s)]


def fit_ready(rows) -> bool:
    """Enough labelled candidates, of both kinds, to fit on."""
    y = [label for _, label in rows]
    return (len(y) >= FIT_MIN and sum(v == 0 for v in y) >= FIT_MIN_EACH
            and sum(v > 0 for v in y) >= FIT_MIN_EACH)


def fit_weights(rows, current):
    """The weights one step toward what the record supports, or None when it does not clearly contradict them."""
    if not fit_ready(rows):
        return None
    keys = list(current)
    y = np.array([label for _, label in rows])
    X = np.array([[s[k] * (1 - s["dead"]) for k in keys] for s, _ in rows])
    w, ones = np.array([current[k] for k in keys]), np.ones(len(y))

    def rss(A):
        beta, *_ = np.linalg.lstsq(A, y, rcond=None)
        return float(((y - A @ beta) ** 2).sum()), beta

    restricted, _ = rss(np.column_stack([ones, X @ w]))       # y from the current rank alone
    free, beta = rss(np.column_stack([ones, X]))              # y from the six answers freely
    extra = len(keys) - 1
    if free <= 0 or (restricted - free) / extra / (free / (len(y) - extra - 2)) < F_MIN:
        return None                                           # the record does not clearly contradict the weights
    v = np.clip(beta[1:], 0, None)
    if v.sum() <= 0:
        return None
    v /= v.sum()
    for _ in range(50):                                       # inside the range and summing to 1 at once
        v = np.clip(v, *WEIGHT_RANGE)
        v /= v.sum()
    step = v - w
    step *= min(1.0, WEIGHT_STEP / max(float(np.abs(step).max()), 1e-12))
    return {k: round(float(x), 4) for k, x in zip(keys, w + step)}


def bars(ranks, seen, kept, capacity):
    if len(ranks) < BAR_MIN_CANDIDATES:
        return None
    keep = max(KEEP_FLOOR, kept / seen) if seen >= 5 else KEEP_DEFAULT
    f = min(1.0, min(capacity / keep, EDITOR_BUDGET) / len(ranks))
    slot = min(max(float(np.quantile(ranks, 1 - f)), SLOT_BAR[0]), SLOT_BAR[1])
    fast = min(max(float(np.quantile(ranks, 1 - f / 3)), slot + FAST_GAP), FAST_MAX)
    return round(slot, 3), round(fast, 3)


def explore_due(ldb, now: float) -> bool:
    """True when enough normal public posts have gone out since the last exploration post and the last attempt
    was over an hour ago."""
    last = flag(ldb, "explore_at")
    if last is not None and now - float(last) < EXPLORE_WAIT_S:
        return False
    normal = ldb.execute(
        "select count(*) from candidates where video_id is not null and explore = 0 and posted_at >"
        " coalesce((select max(posted_at) from candidates where explore = 1 and video_id is not null), -1)"
    ).fetchone()[0]
    return normal >= EXPLORE_EVERY


_EXAMPLE = "{} \u2014 context: {}; event: {}; reaction: {}; why it works: one of this channel's most-watched clips."


def examples(ldb, now: float) -> tuple[str, ...] | None:
    """The best recent posts, formatted for the editor's prompt, or None while there are too few results."""
    p = percentiles(ldb, now)
    if len(p) < WIN_MIN_RESULTS:
        return None
    rows = ldb.execute("select video_id, title, context, what, reaction, posted_at from candidates"
                       " where video_id is not null and what != ''")
    wins = sorted((r for r in rows if p.get(r["video_id"], 0) >= WIN_P),
                  key=lambda r: (-p[r["video_id"]], -r["posted_at"]))
    if len(wins) < WIN_MIN:
        return None
    return tuple(_EXAMPLE.format(*(r[k][:FIELD_MAX] for k in ("title", "context", "what", "reaction")))
                 for r in wins[:WIN_TOP])


def state(ldb) -> dict:
    """The learned values: weights, min_rank, fast_lane and examples, each present only when learned."""
    return json.loads(flag(ldb, "state") or "{}")


def _log(ldb, now: float, what: str, before, after, why: str) -> None:
    ldb.execute("insert into changes (at, what, before, after, why) values (?,?,?,?,?)",
                (now, what, json.dumps(before), json.dumps(after), why))


def update(ldb, capacity: int, now: float) -> None:
    """Refit the weights (once a day), the bars and the examples from the record; does nothing while frozen.
    Everything is worked out first and written in one short transaction, so a posting worker reading the learned
    values never waits for a fit."""
    if flag(ldb, "frozen") == "1":
        return
    st, logs, fitted_at = state(ldb), [], None
    weights = st.get("weights", dict(jev.WEIGHTS))
    at = flag(ldb, "weights_at")
    if at is None or now - float(at) >= FIT_EVERY_S:
        rows = labels(ldb, now)
        if fit_ready(rows):                        # the day's one test, whatever it finds: retesting the same
            fitted_at = now                        # record every pass would let noise through the gate
            fitted = fit_weights(rows, weights)
            if fitted is not None and fitted != weights:
                logs.append(("weights", weights, fitted, f"{len(rows)} labelled candidates"))
                st["weights"] = weights = fitted
    recent = ldb.execute("select scores, kept from candidates where created >= ?", (now - BAR_WINDOW_S,)).fetchall()
    seen, kept = sum(r["kept"] is not None for r in recent), sum(r["kept"] == 1 for r in recent)
    scored = [s for s in (json.loads(r["scores"]) for r in recent) if _complete(s)]
    pair = bars([jev.rank(s, weights=weights) for s in scored], seen, kept, capacity)
    row = ldb.execute("select after from changes where what = 'bars' order by rowid desc limit 1").fetchone()
    logged = json.loads(row["after"]) if row else None
    if pair:
        st["min_rank"], st["fast_lane"] = pair
    else:
        st.pop("min_rank", None), st.pop("fast_lane", None)
    moved = pair and logged and max(abs(a - b) for a, b in zip(pair, logged)) >= BAR_LOG_STEP - 1e-9
    if (logged is None) != (pair is None) or moved:
        logs.append(("bars", logged, pair, f"{len(recent)} candidates in 24 h, editor kept {kept} of {seen}"))
    top = examples(ldb, now)
    learned = list(top) if top else None
    if learned != st.get("examples"):
        logs.append(("examples", st.get("examples"), learned, "best-performing posts of 14 days"))
    if learned:
        st["examples"] = learned
    else:
        st.pop("examples", None)
    with ldb:
        ldb.execute("begin immediate")
        if flag(ldb, "frozen") == "1":             # frozen or reset from the command line while this was working
            return
        for what, before, after, why in logs:
            _log(ldb, now, what, before, after, why)
        if fitted_at is not None:
            flag(ldb, "weights_at", str(fitted_at))
        flag(ldb, "state", json.dumps(st))


def freeze(ldb, on: bool) -> None:
    flag(ldb, "frozen", "1" if on else "0")


def reset(ldb, now: float) -> None:
    """Back to the hand-set values, and frozen there (the next update would otherwise learn them again); the
    record stays."""
    with ldb:
        ldb.execute("begin immediate")
        _log(ldb, now, "reset", state(ldb), None, "hand-set values restored; frozen")
        ldb.execute("delete from kv where key in ('state', 'weights_at')")
        flag(ldb, "frozen", "1")


def changes(ldb, limit: int = 10) -> list[sqlite3.Row]:
    return ldb.execute("select * from changes order by rowid desc limit ?", (limit,)).fetchall()
