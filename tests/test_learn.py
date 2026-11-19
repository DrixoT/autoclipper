import json
import sqlite3

import numpy as np
import pytest

from autoclipper import jev
from autoclipper.learn import (WEIGHT_RANGE, WEIGHT_STEP, add_candidate, add_snapshot, bars, changes, due_snapshots,
                               examples, explore_due, fit_weights, freeze, import_run, labels, mark_explore,
                               open_learning, percentiles, reset, set_posted, set_verdict, state, update)
from autoclipper.store import add_clip, open_db, save_scores, set_state

S = {"hook": .5, "context": .5, "payoff": .5, "crowd": .5, "appeal": .5, "story": .5, "dead": 0.0}


def ldb(tmp_path):
    return open_learning(tmp_path / "learning.db")


def cand(db, cid, **over):
    kw = dict(stream="twitch:a", game="G", mood="funny", det_score=4.0, scores=S, created=1000.0) | over
    add_candidate(db, cid, **kw)


def post(db, cid, vid, at, profile="backup", **over):
    kw = dict(profile=profile, variant="box", video_id=vid, title="A title", what="Something happens",
              context="Setup", reaction="Shock", length_s=30.0, posted_at=at) | over
    set_posted(db, cid, **kw)


def result(db, vid, views, posted_at, watched=None):
    add_snapshot(db, vid, 24, views, 0, posted_at + 24 * 3600, watched)


# --- the record

def test_candidate_is_recorded_once(tmp_path):
    db = ldb(tmp_path); cand(db, "c1"); cand(db, "c1", mood="hype")
    row = db.execute("select * from candidates").fetchall()
    assert len(row) == 1 and row[0]["mood"] == "funny" and row[0]["kept"] is None
    assert json.loads(row[0]["scores"])["hook"] == 0.5

def test_verdict_and_post(tmp_path):
    db = ldb(tmp_path); cand(db, "c1"); cand(db, "c2")
    set_verdict(db, "c1", False); post(db, "c2", "v2", 5000.0)
    kept = {r["clip_id"]: r["kept"] for r in db.execute("select clip_id, kept from candidates")}
    assert kept == {"c1": 0, "c2": 1}

def test_due_snapshots_by_age(tmp_path):
    db = ldb(tmp_path); cand(db, "c1"); post(db, "c1", "v1", 0.0)
    assert due_snapshots(db, 3599.0) == {}
    assert due_snapshots(db, 7 * 3600.0) == {"backup": [("v1", 1, 0.0), ("v1", 6, 0.0)]}
    add_snapshot(db, "v1", 1, 10, 1, 3700.0)
    assert due_snapshots(db, 7 * 3600.0) == {"backup": [("v1", 6, 0.0)]}

def test_a_snapshot_missed_by_a_day_is_skipped(tmp_path):
    db = ldb(tmp_path); cand(db, "c1"); post(db, "c1", "v1", 0.0)
    assert due_snapshots(db, 26 * 3600.0) == {"backup": [("v1", 6, 0.0), ("v1", 24, 0.0)]}   # 1 h is a day late
    assert due_snapshots(db, 97 * 3600.0) == {}                       # the post is past every age


def test_mark_explore(tmp_path):
    db = ldb(tmp_path); cand(db, "c1"); mark_explore(db, "c1", 42.0)
    assert db.execute("select explore from candidates").fetchone()[0] == 1
    assert db.execute("select value from kv where key = 'explore_at'").fetchone()[0] == "42.0"

def test_open_learning_does_not_create_the_parent(tmp_path):
    with pytest.raises(Exception):
        open_learning(tmp_path / "missing" / "learning.db")
    assert not (tmp_path / "missing").exists()

def test_import_run(tmp_path):
    run = open_db(tmp_path / "autoclipper.db")
    a = add_clip(run, "twitch:a", 10, 20, 4.0, "funny", [], "/x/raw.ts", 0)      # uploaded
    b = add_clip(run, "twitch:a", 30, 40, 4.0, "hype", [], "/x/raw.ts", 0)       # editor drop
    c = add_clip(run, "twitch:a", 50, 60, 4.0, "hype", [], "/x/raw.ts", 0)       # below the bar
    for cid in (a, b, c):
        save_scores(run, cid, {"scores": S, "answers": {}}, 0.5, 1, "m")
    set_state(run, a, "uploaded", video_id="v1", uploaded_at=99.0, title="T", youtube_profile="backup",
              variant="box", privacy="public")
    set_state(run, b, "dropped", error="editor: nothing happens")
    set_state(run, c, "dropped")
    run.close()
    db = ldb(tmp_path)
    assert import_run(db, tmp_path / "autoclipper.db") == 3
    assert import_run(db, tmp_path / "autoclipper.db") == 0
    rows = {r["clip_id"]: r for r in db.execute("select * from candidates")}
    assert (rows[a]["kept"], rows[a]["video_id"], rows[a]["posted_at"], rows[a]["profile"]) == (1, "v1", 99.0, "backup")
    assert rows[b]["kept"] == 0 and rows[c]["kept"] is None


# --- the label

def test_import_run_from_before_the_privacy_column(tmp_path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript(
        "create table clips (id text primary key, stream text, score real, mood text, state text, video_id text,"
        " uploaded_at real, created real, error text, title text, youtube_profile text, variant text);"
        "create table scores (clip_id text primary key, answers text, rank real);"
        "insert into clips values ('a', 'twitch:a', 4.0, 'funny', 'uploaded', 'v1', 99.0, 1.0, null, 'T', 'backup', null);")
    old.execute("insert into scores values ('a', ?, 0.5)", (json.dumps({"scores": S}),))
    old.commit(); old.close()
    db = ldb(tmp_path)
    assert import_run(db, tmp_path / "old.db") == 1
    row = db.execute("select kept, video_id, profile from candidates").fetchone()
    assert tuple(row) == (1, "v1", "backup")


def test_labels(tmp_path):                       # dropped 0.0, pending 0.7, posted 0.4 + 0.6 p within its channel
    db = ldb(tmp_path); now = 30 * 3600.0
    cand(db, "d"); set_verdict(db, "d", False)
    cand(db, "u")                                # never seen by the editor: no label
    for i in range(5):
        cand(db, f"p{i}", stream=f"twitch:s{i}"); post(db, f"p{i}", f"v{i}", 0.0)
        result(db, f"v{i}", 100 * (i + 1), 0.0)
    cand(db, "late"); post(db, "late", "vl", now - 60.0)
    ys = sorted(y for _, y in labels(db, now))
    assert ys == pytest.approx([0.0, 0.46, 0.58, 0.7, 0.7, 0.82, 0.94])

def test_percentile_within_stream_when_it_has_five(tmp_path):     # a small streamer's best post is a winner
    db = ldb(tmp_path)
    for i in range(5):
        cand(db, f"b{i}", stream="twitch:big"); post(db, f"b{i}", f"vb{i}", 0.0); result(db, f"vb{i}", 10_000 + i, 0.0)
        cand(db, f"s{i}", stream="twitch:small"); post(db, f"s{i}", f"vs{i}", 0.0); result(db, f"vs{i}", 10 + i, 0.0)
    p = percentiles(db, 30 * 3600.0)
    assert p["vs4"] == pytest.approx(0.9) and p["vb0"] == pytest.approx(0.1)

def test_late_or_old_results_do_not_count(tmp_path):
    db = ldb(tmp_path); now = 30 * 86400.0; at = now - 30 * 3600.0
    for i in range(5):
        cand(db, f"c{i}"); post(db, f"c{i}", f"v{i}", at); result(db, f"v{i}", 100 + i, at)
    cand(db, "late"); post(db, "late", "vlate", at); add_snapshot(db, "vlate", 24, 999, 0, at + 40 * 3600.0)
    cand(db, "old"); post(db, "old", "vold", now - 15 * 86400.0); result(db, "vold", 999, now - 15 * 86400.0)
    assert set(percentiles(db, now)) == {f"v{i}" for i in range(5)}      # neither the late nor the old one

def test_watched_averages_with_views(tmp_path):
    db = ldb(tmp_path)
    for i in range(5):
        cand(db, f"c{i}"); post(db, f"c{i}", f"v{i}", 0.0); result(db, f"v{i}", 100 * (i + 1), 0.0, watched=90 - 10 * i)
    assert list(percentiles(db, 30 * 3600.0).values()) == pytest.approx([0.5] * 5)
    other = open_learning(tmp_path / "other.db")                      # only four with watched: views alone
    for i in range(5):
        cand(other, f"c{i}"); post(other, f"c{i}", f"v{i}", 0.0)
        result(other, f"v{i}", 100 * (i + 1), 0.0, watched=90 - 10 * i if i else None)
    assert percentiles(other, 30 * 3600.0)["v0"] == pytest.approx(0.1)


# --- the fit

def test_fit_moves_toward_the_predictive_answer():
    rng = np.random.default_rng(0); rows = []
    for _ in range(200):
        s = {k: float(rng.uniform()) for k in jev.WEIGHTS} | {"dead": 0.0}
        rows.append((s, 1.0 if s["payoff"] > 0.5 else 0.0))
    w = fit_weights(rows, dict(jev.WEIGHTS))
    assert w["payoff"] == pytest.approx(jev.WEIGHTS["payoff"] + WEIGHT_STEP, abs=0.005)
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)
    assert all(WEIGHT_RANGE[0] - 1e-3 <= x <= WEIGHT_RANGE[1] + 1e-3 for x in w.values())
    assert all(abs(w[k] - jev.WEIGHTS[k]) <= WEIGHT_STEP + 0.005 for k in w)

def test_fit_ignores_noise_and_agreement():
    # labels that are noise, and labels that follow the current weights, almost never move anything
    moved = 0
    for seed in range(20):
        rng = np.random.default_rng(seed)
        scores = [{k: float(rng.uniform(0.2, 0.8)) for k in jev.WEIGHTS} | {"dead": 0.0} for _ in range(100)]
        noise = [(s, float(rng.choice([0.0, 0.0, 0.5, 0.7, 0.9]))) for s in scores]
        agree = [(s, 1.0 if jev.rank(s) + rng.normal(0, 0.05) > 0.5 else 0.0) for s in scores]
        moved += fit_weights(noise, dict(jev.WEIGHTS)) is not None
        moved += fit_weights(agree, dict(jev.WEIGHTS)) is not None
    assert moved <= 2                              # of 40 fits

def test_fit_needs_both_classes():
    s = {k: 0.5 for k in jev.WEIGHTS} | {"dead": 0.0}
    assert fit_weights([(s, 0.7)] * 100, dict(jev.WEIGHTS)) is None       # nothing dropped
    assert fit_weights([(s, 0.0)] * 29, dict(jev.WEIGHTS)) is None        # too few


# --- the bars

def test_bars_rise_on_a_busy_day():
    quiet = [i / 40 for i in range(40)]              # 40 candidates, capacity 48: everything fits
    busy = [i / 400 for i in range(400)]             # 400 candidates: only the top fifth fits the editor budget
    assert bars(quiet, seen=10, kept=5, capacity=48) == (0.35, 0.65)     # floor; the top third posts at once
    assert bars(busy, seen=10, kept=5, capacity=48) == (0.55, 0.8)       # both at their ceilings

def test_bars_fallback_and_clamps():
    assert bars([0.5] * 29, 10, 5, 48) is None
    assert bars([0.5] * 100, 10, 5, 48) == (0.5, 0.55)       # all equal: slot at the value, fast one gap above
    assert bars([0.9] * 100, 0, 0, 48) == (0.55, 0.8)        # clamps hold with nobody seen


# --- exploration and examples

def test_explore_due(tmp_path):
    db = ldb(tmp_path); now = 20_000.0
    assert not explore_due(db, now)                                   # no posts
    for i in range(8):
        cand(db, f"n{i}"); post(db, f"n{i}", f"vn{i}", float(i))
    assert not explore_due(db, now)                                   # 8 normal posts
    cand(db, "n8"); post(db, "n8", "vn8", 8.0)
    assert explore_due(db, now)                                       # 9 normal posts
    cand(db, "x"); post(db, "x", "vx", 100.0); mark_explore(db, "x", 90.0)
    assert not explore_due(db, now)                                   # the exploration post resets the count
    for i in range(9, 18):
        cand(db, f"n{i}"); post(db, f"n{i}", f"vn{i}", 200.0 + i)
    assert explore_due(db, now)                                       # 9 more normal posts since it
    assert not explore_due(db, 90.0 + 600)                            # an attempt ten minutes ago

def test_explore_attempt_waits_an_hour(tmp_path):
    db = ldb(tmp_path)
    for i in range(9):
        cand(db, f"n{i}"); post(db, f"n{i}", f"vn{i}", float(i))
    cand(db, "y"); mark_explore(db, "y", 5000.0)                      # an attempt the editor dropped: no post
    assert not explore_due(db, 5000.0 + 1800)                         # 30 minutes ago
    assert explore_due(db, 5000.0 + 3660)                             # 61 minutes ago

def _posts(db, n, views, *, start=0, at=None, **over):
    for i in range(n):
        cid, vid = f"c{start + i}", f"v{start + i}"
        when = float(start + i) if at is None else at[i]
        cand(db, cid); post(db, cid, vid, when, **{"title": f"T{start + i}", "context": "C", "what": "W",
                                                   "reaction": "R"} | over)
        result(db, vid, views[i], when)

def _text(title, context="C"):
    return (f"{title} \u2014 context: {context}; event: W; reaction: R; why it works: "
            "one of this channel's most-watched clips.")

def test_examples(tmp_path):
    now = 30 * 3600.0
    few = ldb(tmp_path); _posts(few, 9, [100 * (i + 1) for i in range(9)])
    assert examples(few, now) is None                                 # 9 results
    db = open_learning(tmp_path / "ten.db"); _posts(db, 10, [100 * (i + 1) for i in range(10)])
    set_posted(db, "c9", profile="backup", variant="box", video_id="v9", title="T9", what="W",
               context="x" * 300, reaction="R", length_s=30.0, posted_at=9.0)
    assert examples(db, now) == (_text("T9", "x" * 200), _text("T8"))  # p 0.95 then 0.85; context cut to 200
    ties = open_learning(tmp_path / "ties.db")
    _posts(ties, 8, [100 * (i + 1) for i in range(8)])
    _posts(ties, 2, [5000, 5000], start=8, at=[100.0, 200.0])
    assert examples(ties, now) == (_text("T9"), _text("T8"))          # tied p: newest first
    imp = open_learning(tmp_path / "imp.db"); _posts(imp, 12, [100 * (i + 1) for i in range(12)])
    cand(imp, "imp"); post(imp, "imp", "vimp", 50.0, title="Imported", what="", context="", reaction="")
    result(imp, "vimp", 10**6, 50.0)
    got = examples(imp, now)
    assert got == (_text("T11"), _text("T10"))                        # an imported post is never an example


# --- update, freeze, reset

def _fill(db, n, now, hook=None, offset=0):
    for i in range(n):
        cand(db, f"u{offset + i}", created=now - i * 60.0, scores=S | {"hook": i / n if hook is None else hook})

def test_update_logs_and_freeze_and_reset(tmp_path):
    db = ldb(tmp_path); now = 100_000.0
    _fill(db, 40, now)
    update(db, 48, now)
    st = state(db)
    assert "min_rank" in st and "fast_lane" in st and "weights" not in st
    assert [c["what"] for c in changes(db)] == ["bars"]
    update(db, 48, now + 60)
    assert [c["what"] for c in changes(db)] == ["bars"]               # the same data logs nothing
    freeze(db, True); _fill(db, 40, now, hook=1.0, offset=100)
    update(db, 48, now + 120)
    assert state(db) == st                                            # frozen: nothing changes
    freeze(db, False); update(db, 48, now + 180)
    assert state(db) != st
    reset(db, now + 240)
    assert state(db) == {} and changes(db)[0]["what"] == "reset"
    update(db, 48, now + 300)
    assert state(db) == {}                                            # a reset holds until it is unfrozen
    freeze(db, False); update(db, 48, now + 360)
    assert "min_rank" in state(db)

def test_update_fits_the_weights_once_a_day(tmp_path):
    db = ldb(tmp_path); now = 100_000.0; rng = np.random.default_rng(1)
    for i in range(60):
        s = {k: float(rng.uniform()) for k in jev.WEIGHTS} | {"dead": 0.0}
        cand(db, f"w{i}", scores=s, created=now - i * 60.0); set_verdict(db, f"w{i}", s["payoff"] > 0.5)
    update(db, 48, now)
    assert state(db)["weights"]["payoff"] > jev.WEIGHTS["payoff"]
    assert [c["what"] for c in changes(db) if c["what"] == "weights"] == ["weights"]
    update(db, 48, now + 3600)
    assert len([c for c in changes(db) if c["what"] == "weights"]) == 1   # not again within 24 hours


def test_records_from_before_a_question_existed_are_left_out(tmp_path):
    db = ldb(tmp_path); now = 100_000.0
    old = {k: v for k, v in S.items() if k != "story"}                 # scored before the story question
    _fill(db, 40, now)
    for i in range(40):
        cand(db, f"old{i}", scores=old, created=now - i); set_verdict(db, f"old{i}", False)
    assert all("story" in s for s, _ in labels(db, now))
    update(db, 48, now)                                               # no KeyError; bars from the 40 complete ones
    assert "min_rank" in state(db)


def test_a_fit_that_finds_nothing_still_uses_the_day(tmp_path):
    db = ldb(tmp_path); now = 100_000.0; rng = np.random.default_rng(3)
    for i in range(60):                                               # verdicts unrelated to the scores
        s = {k: float(rng.uniform(0.2, 0.8)) for k in jev.WEIGHTS} | {"dead": 0.0}
        cand(db, f"n{i}", scores=s, created=now - i * 60.0); set_verdict(db, f"n{i}", i % 2 == 0)
    update(db, 48, now)
    assert "weights" not in state(db)
    assert db.execute("select value from kv where key = 'weights_at'").fetchone()[0] == str(now)


def test_too_few_labels_do_not_use_the_day(tmp_path):
    db = ldb(tmp_path); cand(db, "c1"); set_verdict(db, "c1", False)
    update(db, 48, 100_000.0)
    assert db.execute("select value from kv where key = 'weights_at'").fetchone() is None
