import time
import json
from autoclipper.store import (add_clip, bump_llm, cut_rows, expire, flag, llm_calls, open_db, pick_variant,
                               recover, set_state, uploaded_ids, uploads_since)


def _clip(db, score, onset=100.0):
    return add_clip(db, "twitch:a", onset, onset + 20, score, "funny", ["LUL"], "/x/raw.ts", onset - 30)

def test_recover_marks_uploading_failed_and_rendering_cut(tmp_path):
    db = open_db(tmp_path / "a.db")
    a, b = _clip(db, 1.0), _clip(db, 2.0)
    set_state(db, a, "uploading"); set_state(db, b, "rendering")
    assert recover(db) == 2
    rows = {r["id"]: r["state"] for r in db.execute("select id, state from clips")}
    assert rows == {a: "failed", b: "cut"}

def test_expire_drops_old_cut_rows_and_returns_paths(tmp_path):
    db = open_db(tmp_path / "a.db"); _clip(db, 1.0, onset=0.0)      # an hour-old moment, queued just now
    assert expire(db, now=time.time(), max_age_s=3600) == []
    assert len(cut_rows(db, now=time.time(), max_age_s=3600)) == 1
    assert expire(db, now=time.time() + 4000, max_age_s=3600) == ["/x/raw.ts"]
    assert cut_rows(db, now=time.time() + 4000, max_age_s=99999) == []

def test_flags_and_llm_counter(tmp_path):
    db = open_db(tmp_path / "a.db")
    assert flag(db, "uploads_paused") is None
    flag(db, "uploads_paused", "strike"); assert flag(db, "uploads_paused") == "strike"
    assert bump_llm(db, "luna", "2026-11-17") == 1 and bump_llm(db, "luna", "2026-11-17") == 2
    assert llm_calls(db, "luna", "2026-11-18") == 0

def test_uploads_since_counts_only_uploaded(tmp_path):
    db = open_db(tmp_path / "a.db"); a = _clip(db, 1.0); _clip(db, 2.0)
    set_state(db, a, "uploaded", video_id="v1", uploaded_at=500.0)
    assert uploads_since(db, 400.0) == 1 and uploaded_ids(db) == ["v1"]


# --- round 3: candidates, Jev scores and the last upload

def test_cut_rows_lists_recent_cut_clips_by_score(tmp_path):
    from autoclipper.store import cut_rows
    db = open_db(tmp_path / "a.db"); a = _clip(db, 5.0); b = _clip(db, 9.0); old = _clip(db, 12.0, onset=-5000.0)
    set_state(db, a, "uploaded", video_id="v", uploaded_at=1.0)
    c = _clip(db, 1.0)
    db.execute("update clips set created = created - 5000 where id = ?", (old,))   # queued too long ago
    assert [r["id"] for r in cut_rows(db, now=time.time(), max_age_s=3600)] == [b, c]

def test_scores_are_stored_once_per_clip(tmp_path):
    from autoclipper.store import get_scores, save_scores
    db = open_db(tmp_path / "a.db"); a = _clip(db, 5.0)
    assert get_scores(db, a) is None
    save_scores(db, a, {"hook": 0.5}, 0.61, 4321, "jev-1.14.0")
    got = get_scores(db, a)
    assert got["rank"] == 0.61 and got["input_tokens"] == 4321 and json.loads(got["answers"]) == {"hook": 0.5}

def test_last_upload_time(tmp_path):
    from autoclipper.store import last_upload
    db = open_db(tmp_path / "a.db"); a = _clip(db, 5.0); b = _clip(db, 6.0)
    assert last_upload(db) is None
    set_state(db, a, "uploaded", video_id="v1", uploaded_at=100.0); set_state(db, b, "uploaded", video_id="v2", uploaded_at=250.0)
    assert last_upload(db) == 250.0


def test_existing_upload_history_defaults_to_primary(tmp_path):
    path = tmp_path / "a.db"
    db = open_db(path)
    cid = _clip(db, 1.0)
    set_state(db, cid, "uploaded", video_id="legacy", uploaded_at=1.0)
    db.execute("alter table clips drop column youtube_profile")
    db.close()

    db = open_db(path)
    assert uploaded_ids(db) == ["legacy"]
    assert uploaded_ids(db, profile="backup") == []


def _companion(db, cid, state="pending", profile="primary", **kw):
    from autoclipper.store import put_output
    return put_output(db, cid, "landscape", state, {"title": "t"}, "/x/landscape.mp4", "/x/landscape.jpg", profile, **kw)


def test_companions_count_toward_cap_gap_and_strike_check(tmp_path):
    from autoclipper.store import last_upload, set_output
    db = open_db(tmp_path / "a.db"); a = _clip(db, 1.0)
    set_state(db, a, "uploaded", video_id="short1", uploaded_at=100.0)
    _companion(db, a)
    assert uploads_since(db, 0) == 1 and last_upload(db) == 100.0     # pending does not count
    set_output(db, a, "landscape", "uploaded", video_id="wide1", uploaded_at=200.0)
    assert uploads_since(db, 0) == 2 and last_upload(db) == 200.0
    assert uploaded_ids(db) == ["wide1", "short1"] and uploaded_ids(db, profile="backup") == []


def test_an_uploading_or_uploaded_companion_is_never_replaced_and_recover_fails_it(tmp_path):
    from autoclipper.store import get_output, set_output
    db = open_db(tmp_path / "a.db"); a, b = _clip(db, 1.0), _clip(db, 2.0)
    _companion(db, a); set_output(db, a, "landscape", "uploaded", video_id="wide1", uploaded_at=1.0)
    _companion(db, b); set_output(db, b, "landscape", "uploading")
    assert not _companion(db, a) and not _companion(db, b)            # a clip processed again cannot repost
    assert get_output(db, a, "landscape")["video_id"] == "wide1"
    assert recover(db) == 1 and get_output(db, b, "landscape")["state"] == "failed"
    assert _companion(db, a, "failed") is False and _companion(db, _clip(db, 3.0)) is True


def test_next_companion_waits_for_its_short_and_its_profile_and_expires(tmp_path):
    from autoclipper.store import get_output, next_output
    db = open_db(tmp_path / "a.db"); a, b = _clip(db, 1.0), _clip(db, 2.0)
    _companion(db, a); _companion(db, b, profile="backup")
    assert next_output(db, "primary", 0) is None                       # its Short is not up yet
    set_state(db, a, "uploaded", video_id="s", uploaded_at=1.0)
    set_state(db, b, "uploaded", video_id="t", uploaded_at=1.0)
    assert next_output(db, "backup", 0) is None                       # the Short was posted on another channel
    set_state(db, b, "uploaded", youtube_profile="backup")
    assert next_output(db, "primary", 0)["clip_id"] == a and next_output(db, "backup", 0)["clip_id"] == b
    assert next_output(db, "primary", 10**12) is None and get_output(db, a, "landscape")["state"] == "dropped"


def test_variants_alternate_by_posted_clips_only(tmp_path):
    db = open_db(tmp_path / "a.db")
    ids = [add_clip(db, "twitch:a", t, t + 5, 1.0, "funny", ["x"], f"/x/{t}.ts", t - 30) for t in range(4)]
    assert pick_variant(db, ("box", "emoji")) == "box"       # ties go to list order
    set_state(db, ids[0], "uploaded", video_id="v", uploaded_at=1.0, variant="box")
    set_state(db, ids[1], "failed", variant="emoji")
    set_state(db, ids[2], "uploading", variant="emoji")
    assert pick_variant(db, ("box", "emoji")) == "emoji"     # neither the failed nor the uploading one counts
    set_state(db, ids[3], "saved", uploaded_at=2.0, variant="emoji")
    assert pick_variant(db, ("box", "emoji")) == "box"
    assert pick_variant(db, ("box",)) == "box"


def test_existing_database_gains_a_null_variant(tmp_path):
    path = tmp_path / "a.db"
    db = open_db(path)
    cid = _clip(db, 1.0)
    set_state(db, cid, "uploaded", video_id="legacy", uploaded_at=1.0)
    db.execute("alter table clips drop column variant")
    db.close()

    db = open_db(path)
    assert db.execute("select variant from clips where id = ?", (cid,)).fetchone()[0] is None
    assert pick_variant(db, ("box", "emoji")) == "box"       # an old row without a variant counts for neither


def test_cap_gap_and_pause_are_counted_per_profile(tmp_path):
    from autoclipper.store import last_upload, pause, paused
    db = open_db(tmp_path / "a.db"); a, b = _clip(db, 1.0), _clip(db, 2.0)
    set_state(db, a, "uploaded", video_id="p", uploaded_at=100.0)
    set_state(db, b, "uploaded", video_id="q", uploaded_at=200.0, youtube_profile="backup")
    assert uploads_since(db, 0) == 2 and last_upload(db) == 200.0          # no profile: every channel
    assert uploads_since(db, 0, profile="primary") == 1 and last_upload(db, profile="primary") == 100.0
    assert uploads_since(db, 150.0, profile="backup") == 1 and last_upload(db, profile="other") is None
    assert paused(db, "primary") == "" and paused(db, "backup") == ""
    pause(db, "primary", "limit:9")
    assert paused(db, "primary") == "limit:9" and paused(db, "backup") == ""
    flag(db, "uploads_paused", "strike:v")                 # the old global key is migrated at startup, not read
    assert paused(db, "backup") == ""


def test_no_variant_when_no_channel_can_post(tmp_path):
    assert pick_variant(open_db(tmp_path / "a.db"), ()) is None


def test_the_old_global_pause_moves_into_every_profile_once_and_a_strike_wins(tmp_path):
    from autoclipper.store import migrate_pause, pause, paused
    db = open_db(tmp_path / "a.db")
    flag(db, "uploads_paused", "strike:v"); pause(db, "primary", "limit:9")
    migrate_pause(db, ("primary", "backup"), "primary")
    assert paused(db, "primary") == paused(db, "backup") == "strike:v" and flag(db, "uploads_paused") == ""
    flag(db, "uploads_paused", "limit:5"); pause(db, "backup", "strike:x"); pause(db, "primary", "")
    migrate_pause(db, ("primary", "backup"), "primary")
    assert (paused(db, "primary"), paused(db, "backup")) == ("limit:5", "strike:x")   # a limit never hides a strike
    migrate_pause(db, ("primary", "backup"), "primary")                                         # nothing left to move
    assert (paused(db, "primary"), paused(db, "backup")) == ("limit:5", "strike:x")


def test_routed_balance_counts_posts_on_each_variants_channel_only(tmp_path):
    db = open_db(tmp_path / "a.db")
    for variant, profile in (("box", "backup"), ("box", "primary"), ("box", "primary"), ("box", "primary"),
                             ("emoji", "primary"), ("emoji", "primary")):
        set_state(db, _clip(db, 1.0), "uploaded", video_id="v", uploaded_at=1.0, variant=variant, youtube_profile=profile)
    assert pick_variant(db, ("box", "emoji")) == "emoji"                              # unrouted: 4 box, 2 emoji
    assert pick_variant(db, ("box", "emoji"), {"box": "backup", "emoji": "primary"}) == "box"   # 1 on backup, 2


def test_one_connection_survives_many_threads(tmp_path):
    import threading
    db = open_db(tmp_path / "a.db")
    for i in range(20):
        add_clip(db, "twitch:a", float(i), i + 5.0, 1.0, "funny", ["x"], f"/x/{i}.ts", i - 30.0)
    errors = []
    def hammer():
        try:
            for _ in range(300):
                flag(db, "uploads_paused:backup")
                db.execute("select * from clips").fetchall()
                flag(db, "last_slot", "1")
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for th in threads: th.start()
    for th in threads: th.join()
    assert errors == []

def test_automatic_pauses_never_replace_a_strike(tmp_path):
    from autoclipper.store import pause, paused
    db = open_db(tmp_path / "a.db")
    assert pause(db, "primary", "strike:v") is True
    for why in ("auth", "limit:9", "duplicate:UC-x"):
        assert pause(db, "primary", why) is False and paused(db, "primary") == "strike:v"
    assert pause(db, "primary", "strike:w") is True and paused(db, "primary") == "strike:w"   # a newer strike
    assert pause(db, "backup", "auth") is True and pause(db, "backup", "auth") is False      # nothing changed
    assert pause(db, "backup", "limit:9") is True and paused(db, "backup") == "limit:9"
    pause(db, "primary", "")                                                                 # resume lifts anything
    assert paused(db, "primary") == ""


def test_lifting_a_pause_clears_only_the_value_that_was_read(tmp_path):
    from autoclipper.store import lift, pause, paused
    db = open_db(tmp_path / "a.db")
    pause(db, "primary", "limit:1")
    assert lift(db, "primary", "limit:1") is True and paused(db, "primary") == ""
    pause(db, "primary", "strike:v")
    assert lift(db, "primary", "limit:1") is False and paused(db, "primary") == "strike:v"


def test_an_old_limit_or_sign_in_pause_moves_only_to_the_channel_that_was_posting(tmp_path):
    """Live 2026-11-17: backup hit YouTube's upload limit under the single-profile config, and the routed restart
    copied that limit onto primary too, which never hit it."""
    from autoclipper.store import migrate_pause, paused, put_output
    db = open_db(tmp_path / "a.db")
    set_state(db, _clip(db, 1.0), "uploaded", video_id="p", uploaded_at=100.0)                     # primary, earlier
    set_state(db, _clip(db, 1.0), "uploaded", video_id="b", uploaded_at=200.0, youtube_profile="backup")
    flag(db, "uploads_paused", "limit:9")
    migrate_pause(db, ("backup", "primary"), "primary")
    assert (paused(db, "backup"), paused(db, "primary")) == ("limit:9", "") and flag(db, "uploads_paused") == ""
    wide = _clip(db, 1.0)                                                # a companion is the latest upload now
    set_state(db, wide, "uploaded", video_id="s", uploaded_at=250.0, youtube_profile="backup")
    put_output(db, wide, "landscape", "uploaded", {}, "/x.mp4", "/x.jpg", "primary", video_id="w", uploaded_at=300.0)
    flag(db, "uploads_paused", "auth")
    migrate_pause(db, ("backup", "primary"), "backup")
    assert paused(db, "primary") == "auth"
    flag(db, "uploads_paused", "strike:v")                               # a strike still pauses every channel
    migrate_pause(db, ("backup", "primary"), "primary")
    assert paused(db, "backup") == paused(db, "primary") == "strike:v"


def test_an_old_limit_with_no_upload_history_goes_to_the_configured_profile(tmp_path):
    from autoclipper.store import migrate_pause, paused
    db = open_db(tmp_path / "a.db")
    flag(db, "uploads_paused", "limit:9")
    migrate_pause(db, ("backup", "primary"), "primary")
    assert (paused(db, "backup"), paused(db, "primary")) == ("", "limit:9")


def test_an_old_limit_moves_to_the_profile_the_old_config_posted_on_when_it_is_known(tmp_path):
    from autoclipper.store import migrate_pause, paused, record_posting_profile
    db = open_db(tmp_path / "a.db")
    record_posting_profile(db, "backup")                                # the single-profile config last run on backup
    set_state(db, _clip(db, 1.0), "uploaded", video_id="p", uploaded_at=200.0)   # ...but primary uploaded last
    flag(db, "uploads_paused", "limit:9")
    migrate_pause(db, ("backup", "primary"), "primary")
    assert (paused(db, "backup"), paused(db, "primary")) == ("limit:9", "")
