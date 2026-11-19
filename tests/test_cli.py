import json
import plistlib
import time
from pathlib import Path

import pytest

from autoclipper.cli import LABEL, main
from autoclipper.store import add_clip, flag, open_db, set_state


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "streams.toml"
    path.write_text(f'data_dir = "{tmp_path}"\nvolume = "{tmp_path}"\n\n[[stream]]\nplatform = "twitch"\nchannel = "a"\n')
    return path


def test_status_prints_stream_and_usage(tmp_path, capsys):
    config = _config(tmp_path)
    db = open_db(tmp_path / "autoclipper.db")
    flag(db, "stream:twitch:a", '{"capture":true,"last_chat":null,"events":[1,2],"updated":1}')
    flag(db, "uploads_paused", "strike:v1")
    add_clip(db, "twitch:a", 1, 2, 1, "funny", [], "/x", 0)
    db.close()

    main(["--config", str(config), "status"])
    output = capsys.readouterr().out
    assert "twitch:a" in output
    assert "capture alive stale" in output
    assert "events last hour 0" in output
    assert "Uploads paused: strike:v1" in output


def test_resume_clears_pause_flag(tmp_path, capsys):
    config = _config(tmp_path)
    db = open_db(tmp_path / "autoclipper.db")
    flag(db, "uploads_paused", "strike:v1")
    db.close()

    main(["--config", str(config), "resume"])
    db = open_db(tmp_path / "autoclipper.db")
    assert flag(db, "uploads_paused") == ""
    assert "resumed" in capsys.readouterr().out
    db.close()


def test_start_writes_its_own_job_and_stop_unloads_it(tmp_path, monkeypatch, capsys):
    import sys
    import autoclipper.cli as cli

    config = _config(tmp_path)
    calls = []
    class Result:
        def __init__(self, code): self.returncode = code
    monkeypatch.setattr(cli.os, "getuid", lambda: 501)
    monkeypatch.setenv("PATH", "/Users/someone/.local/bin:/usr/bin:/bin")
    # launchd refuses a job file on a volume mounted with ownership disabled, so the job goes under ~/Library/LaunchAgents
    monkeypatch.setattr(cli, "LAUNCH_AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(cli, "LAUNCHD_LOGS", tmp_path / "Logs")
    def run(argv, check, capture_output=False):
        calls.append(argv); return Result(113 if argv[1] == "print" else 0)     # 113: not loaded yet
    monkeypatch.setattr(cli.subprocess, "run", run)
    main(["--config", str(config), "start"])
    main(["--config", str(config), "stop"])
    job_path = tmp_path / "LaunchAgents" / f"{LABEL}.plist"
    assert calls == [["launchctl", "print", f"gui/501/{LABEL}"],
                     ["launchctl", "bootstrap", "gui/501", str(job_path)],
                     ["launchctl", "bootout", f"gui/501/{LABEL}"]]
    with job_path.open("rb") as f:
        job = plistlib.load(f)
    # launchd signals this Python directly: no uv run, no caffeinate wrapper, and the config that was selected
    assert job["ProgramArguments"] == [sys.executable, "-m", "autoclipper.cli", "--config", str(config.resolve()), "run"]
    assert job["Label"] == LABEL and job["KeepAlive"] is True
    assert job["EnvironmentVariables"]["PATH"].startswith("/Users/someone/.local/bin:/usr/bin:/bin")
    assert job["WorkingDirectory"] == str(tmp_path)
    # launchd cannot open log files on the ownership-disabled data volume (exit 78), so its two files live elsewhere
    assert job["StandardErrorPath"] == str(tmp_path / "Logs" / "autoclipper.err.log")
    capsys.readouterr()


def test_launchd_template_has_no_machine_paths():
    from importlib import resources
    raw = (resources.files("autoclipper") / "launchd.plist").read_bytes()    # the template ships inside the package
    job = plistlib.loads(raw)
    assert job["Label"] == "com.example.autoclipper" and job["KeepAlive"] is True
    assert "ProgramArguments" not in job and b"/Users/" not in raw


def test_start_refuses_when_the_job_is_already_loaded(tmp_path, monkeypatch, capsys):
    import autoclipper.cli as cli
    calls = []
    class Result:
        returncode = 0
    monkeypatch.setattr(cli, "LAUNCH_AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, check, capture_output=False: calls.append(argv) or Result())
    main(["--config", str(_config(tmp_path)), "start"])
    assert [a[1] for a in calls] == ["print"] and "already loaded" in capsys.readouterr().out


def test_report_prints_each_variant(tmp_path, monkeypatch, capsys):
    cfg = _config(tmp_path)
    db = open_db(tmp_path / "autoclipper.db")
    for vid, variant, age_days in (("v1", "box", 0), ("v3", "box", 1), ("v2", "emoji", 0), ("v4", None, 0),
                                   ("old", "box", 9)):
        cid = add_clip(db, "twitch:a", 1.0, 2.0, 1.0, "funny", ["x"], f"/x/{vid}.ts", 0.0)
        set_state(db, cid, "uploaded", video_id=vid, uploaded_at=time.time() - age_days * 86400, variant=variant)
    other = add_clip(db, "twitch:a", 1.0, 2.0, 1.0, "funny", ["x"], "/x/b.ts", 0.0)
    set_state(db, other, "uploaded", video_id="elsewhere", uploaded_at=time.time(), youtube_profile="backup")
    db.close()
    asked = {}
    monkeypatch.setattr("autoclipper.upload.client", lambda profile: (asked.setdefault("profile", profile), "UC"))
    monkeypatch.setattr("autoclipper.upload.video_stats",
                        lambda yt, ids: asked.setdefault("ids", sorted(ids)) and {"v1": (100, 5), "v3": (40, 1)})
    main(["--config", str(cfg), "report"])
    out = capsys.readouterr().out
    assert asked == {"profile": "primary", "ids": ["v1", "v2", "v3", "v4"]}     # this profile, last 7 days
    assert "box: 2 posted, 140 views, 6 likes, median 70 views" in out
    assert "emoji: 1 posted, 1 without stats" in out and "none: 1 posted, 1 without stats" in out


def test_report_with_nothing_posted_does_not_call_youtube(tmp_path, monkeypatch, capsys):
    cfg = _config(tmp_path)
    open_db(tmp_path / "autoclipper.db").close()
    def no_youtube(profile): raise AssertionError("no YouTube call without uploads")
    monkeypatch.setattr("autoclipper.upload.client", no_youtube)
    main(["--config", str(cfg), "report", "--days", "2"])
    assert "No uploads in the last 2 days" in capsys.readouterr().out


def _routed_config(tmp_path: Path) -> Path:
    path = _config(tmp_path)
    path.write_text('variant_profiles = { box = "backup", emoji = "primary" }\ninstagram_profiles = ["backup"]\n'
                    'instagram_account_id = "42"\n' + path.read_text())
    return path


def test_status_shows_each_channel_with_its_variants_uploads_and_pause(tmp_path, capsys):
    config = _routed_config(tmp_path)
    db = open_db(tmp_path / "autoclipper.db")
    flag(db, "uploads_paused:primary", "limit:9")
    cid = add_clip(db, "twitch:a", 1, 2, 1, "funny", [], "/x", 0)
    set_state(db, cid, "uploaded", video_id="v", uploaded_at=time.time(), youtube_profile="backup", variant="box")
    db.close()
    main(["--config", str(config), "status"])
    out = capsys.readouterr().out
    backup, primary = out.split("YouTube profile: backup")[1], out.split("YouTube profile: primary")[1]
    assert backup.startswith(" (box, Instagram)") and primary.startswith(" (emoji)")
    assert "Uploads last 24h: 1" in backup.split("YouTube profile")[0] and "Uploads paused: no" in backup
    assert "Uploads last 24h: 0" in primary and "Uploads paused: limit:9" in primary


def test_resume_clears_one_channel_or_all(tmp_path, capsys):
    from autoclipper.store import paused
    config = _routed_config(tmp_path)
    db = open_db(tmp_path / "autoclipper.db")
    flag(db, "uploads_paused", "strike:v1")                    # from before the pause was per channel
    db.close()
    main(["--config", str(config), "resume", "--profile", "backup"])
    db = open_db(tmp_path / "autoclipper.db")
    assert paused(db, "backup") == "" and paused(db, "primary") == "strike:v1"
    db.close()
    main(["--config", str(config), "resume"])
    db = open_db(tmp_path / "autoclipper.db")
    assert paused(db, "backup") == paused(db, "primary") == ""
    db.close()
    assert "backup" in capsys.readouterr().out


def test_report_groups_by_channel_and_variant(tmp_path, monkeypatch, capsys):
    cfg = _routed_config(tmp_path)
    db = open_db(tmp_path / "autoclipper.db")
    for vid, variant, profile in (("b1", "box", "backup"), ("e1", "emoji", "primary"), ("e2", "emoji", "primary")):
        cid = add_clip(db, "twitch:a", 1.0, 2.0, 1.0, "funny", ["x"], f"/x/{vid}.ts", 0.0)
        set_state(db, cid, "uploaded", video_id=vid, uploaded_at=time.time(), variant=variant, youtube_profile=profile)
    db.close()
    asked = []
    monkeypatch.setattr("autoclipper.upload.client", lambda profile: (f"yt-{profile}", f"UC-{profile}"))
    monkeypatch.setattr("autoclipper.upload.video_stats",
                        lambda yt, ids: asked.append((yt, sorted(ids))) or {"b1": (10, 1), "e1": (30, 2), "e2": (50, 3)})
    main(["--config", str(cfg), "report"])
    out = capsys.readouterr().out
    assert asked == [("yt-backup", ["b1"]), ("yt-primary", ["e1", "e2"])]
    assert out.index("backup") < out.index("box: 1 posted, 10 views") < out.index("primary")
    assert out.index("primary") < out.index("emoji: 2 posted, 80 views, 5 likes, median 40 views")


def test_report_goes_on_when_one_channel_cannot_sign_in(tmp_path, monkeypatch, capsys):
    cfg = _routed_config(tmp_path)
    db = open_db(tmp_path / "autoclipper.db")
    for vid, variant, profile in (("b1", "box", "backup"), ("e1", "emoji", "primary")):
        cid = add_clip(db, "twitch:a", 1.0, 2.0, 1.0, "funny", ["x"], f"/x/{vid}.ts", 0.0)
        set_state(db, cid, "uploaded", video_id=vid, uploaded_at=time.time(), variant=variant, youtube_profile=profile)
    db.close()
    def client(profile):
        if profile == "backup": raise RuntimeError("token revoked")
        return f"yt-{profile}", f"UC-{profile}"
    monkeypatch.setattr("autoclipper.upload.client", client)
    monkeypatch.setattr("autoclipper.upload.video_stats", lambda yt, ids: {"e1": (30, 2)})
    main(["--config", str(cfg), "report"])
    out = capsys.readouterr().out
    assert "profile backup: 1 posted; statistics unavailable (RuntimeError: token revoked)" in out
    assert "emoji: 1 posted, 30 views, 2 likes" in out


def test_login_opens_the_chosen_channels_chrome_profile(tmp_path, monkeypatch):
    import autoclipper.studio as studio
    opened = []
    monkeypatch.setattr(studio, "launch_login", opened.append)
    main(["--config", str(_routed_config(tmp_path)), "login", "--profile", "backup"])
    main(["--config", str(_routed_config(tmp_path)), "login"])
    assert opened == [tmp_path / "chrome-profiles" / "backup", tmp_path / "chrome-profile"]


def test_status_shows_the_old_global_pause_without_moving_it(tmp_path, capsys):
    config = _routed_config(tmp_path)
    db = open_db(tmp_path / "autoclipper.db")
    flag(db, "uploads_paused", "strike:v1")
    db.close()
    main(["--config", str(config), "status"])
    out = capsys.readouterr().out
    assert out.count("Uploads paused: strike:v1") == 2 and "global" in out
    db = open_db(tmp_path / "autoclipper.db")
    assert flag(db, "uploads_paused") == "strike:v1"
    assert flag(db, "uploads_paused:backup") is None and flag(db, "uploads_paused:primary") is None
    db.close()


# --- autoclipper learn

def _record(tmp_path, n=0, now=None):
    """The learning record next to the config, with n candidates scored in the last day."""
    from autoclipper import learn
    db = learn.open_learning(tmp_path / "learning.db")
    now = now or time.time()
    scores = {"hook": 0.5, "context": 0.5, "payoff": 0.5, "crowd": 0.5, "appeal": 0.5, "story": 0.5, "dead": 0.0}
    for i in range(n):
        learn.add_candidate(db, f"c{i}", "twitch:a", "G", "funny", 4.0, scores | {"hook": i / n}, now - i * 60.0)
    return db, now


def _learn(tmp_path, capsys, *flags, config=None):
    main(["--config", str(config or _config(tmp_path)), "learn", *flags])
    return capsys.readouterr().out


def test_learn_with_an_empty_record(tmp_path, capsys):
    out = _learn(tmp_path, capsys)
    assert str(tmp_path / "learning.db") in out
    assert "candidates 0, editor verdicts 0, posts 0, posts with a 24 h result 0" in out
    assert "hook 0.20 (hand-set 0.20)" in out and "story 0.20 (hand-set 0.20)" in out
    assert "bars: hand-set (fewer than 30 candidates in 24 h)" in out and "slot minimum" not in out
    assert "editor examples: hand-picked" in out and "last changes:" in out and "frozen" not in out
    assert "learn = false" not in out


def test_learn_shows_the_learned_bars_and_their_change(tmp_path, capsys):
    from autoclipper import learn
    db, now = _record(tmp_path, 40)
    learn.update(db, 48, now)
    out = _learn(tmp_path, capsys)
    assert "candidates 40, editor verdicts 0, posts 0" in out
    assert "slot minimum 0.40 (hand-set 0.45)" in out and "post at once 0.53 (configured 0.80)" in out
    lines = out.splitlines()
    assert sum(" bars: " in line for line in lines) == 1 and "bars: hand-set" not in out
    change = next(line for line in lines if " bars: " in line)
    assert change.split()[0].count("-") == 2 and "null -> [0.4, 0.53] (40 candidates in 24 h" in change


def test_learn_shows_learned_weights_and_examples(tmp_path, capsys):
    db, _ = _record(tmp_path)
    flag(db, "state", json.dumps({"weights": {"hook": 0.23, "context": 0.10, "payoff": 0.20, "crowd": 0.10,
                                              "appeal": 0.17, "story": 0.20}, "examples": ["E1", "E2"]}))
    out = _learn(tmp_path, capsys)
    assert "hook 0.23 (hand-set 0.20)" in out and "appeal 0.17 (hand-set 0.20)" in out
    assert "editor examples: learned" in out and "E1" in out and "E2" in out and "hand-picked" not in out


def test_learn_says_when_it_only_records(tmp_path, capsys):
    config = _config(tmp_path)
    config.write_text("learn = false\n" + config.read_text())
    assert _learn(tmp_path, capsys, config=config).splitlines()[0] == (
        "learn = false: recording only, hand-set values in use")


def test_learn_freeze_and_unfreeze(tmp_path, capsys):
    _learn(tmp_path, capsys, "--freeze")
    assert "frozen" in _learn(tmp_path, capsys).splitlines()
    _learn(tmp_path, capsys, "--unfreeze")
    assert "frozen" not in _learn(tmp_path, capsys)


def test_learn_reset_restores_the_hand_set_values_and_freezes(tmp_path, capsys):
    from autoclipper import learn
    db, now = _record(tmp_path, 40)
    learn.update(db, 48, now)
    assert "slot minimum" in _learn(tmp_path, capsys)
    out = _learn(tmp_path, capsys, "--reset")
    assert "hand-set values restored; learning is frozen (autoclipper learn --unfreeze resumes it)" in out
    out = _learn(tmp_path, capsys)
    assert "bars: hand-set" in out and "frozen" in out.splitlines() and "slot minimum" not in out
    assert " reset: " in out and "candidates 40" in out                    # the record itself is kept


def test_learn_import_copies_an_older_run(tmp_path, capsys):
    from autoclipper.store import save_scores
    older = tmp_path / "older"; older.mkdir()
    run = open_db(older / "autoclipper.db")
    ids = [add_clip(run, "twitch:a", 10 * i, 10 * i + 5, 4.0, "funny", [], "/x/raw.ts", 0) for i in range(3)]
    for cid in ids:
        save_scores(run, cid, {"scores": {"hook": .5, "context": .5, "payoff": .5, "crowd": .5, "appeal": .5,
                                          "story": .5, "dead": 0.0}, "answers": {}}, 0.5, 1, "m")
    set_state(run, ids[0], "uploaded", video_id="v1", uploaded_at=99.0, title="T", youtube_profile="backup",
              variant="box", privacy="public")
    set_state(run, ids[1], "dropped", error="editor: nothing happens")
    run.close()
    assert _learn(tmp_path, capsys, "--import", str(older / "autoclipper.db")).strip() == "imported 3 candidates"
    assert "candidates 3, editor verdicts 2, posts 1" in _learn(tmp_path, capsys)
    assert _learn(tmp_path, capsys, "--import", str(older / "autoclipper.db")).strip() == "imported 0 candidates"


def test_learn_flags_exclude_each_other(tmp_path, capsys):
    for flags in (("--freeze", "--reset"), ("--unfreeze", "--freeze"), ("--import", "x.db", "--reset")):
        with pytest.raises(SystemExit) as stop:
            _learn(tmp_path, capsys, *flags)
        assert stop.value.code == 2


def test_auth_analytics_flag_reaches_auth(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("autoclipper.upload.auth",
                        lambda json_file, *, profile, analytics=False: calls.append((profile, analytics)) or {"id": "UC"})
    main(["--config", str(_config(tmp_path)), "auth", "--profile", "backup", "--analytics"])
    main(["--config", str(_config(tmp_path)), "auth", "--profile", "backup"])
    assert calls == [("backup", True), ("backup", False)]
