import argparse
import asyncio
import getpass
from importlib import resources
import json
import os
import plistlib
import statistics
import subprocess
import sys
import time
from pathlib import Path

from .config import DEFAULT_PROFILE, instagram_profiles, learn_path, load, profile_for, profiles, validate_profile
from .meta import PROVIDERS
from .store import PAUSED, flag, llm_calls, migrate_pause, open_db, pause, paused, uploads_since


DEFAULT_CONFIG = Path.home() / "autoclipper-data" / "streams.toml"
TEMPLATE = resources.files("autoclipper") / "launchd.plist"      # shipped inside the package
LABEL = f"com.{getpass.getuser()}.autoclipper"
# launchd refuses a job file on a volume mounted with ownership disabled.
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
# launchd also cannot open the job's stdout/stderr files there (it exits with 78, EX_CONFIG), so they go to the
# internal disk; they only ever hold a crash traceback, because the daemon logs to <data_dir>/logs itself.
LAUNCHD_LOGS = Path.home() / "Library" / "Logs"


def write_job(config: Path) -> Path:
    """The launchd job for this machine, from the template: launchd runs this environment's Python directly (so it
    signals the daemon itself), with the PATH of the shell that ran `start`, where claude and codex live."""
    settings = load(config)
    job = plistlib.loads(TEMPLATE.read_bytes())
    job["Label"] = LABEL
    path = os.environ.get("PATH", "")
    for extra in (str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/bin", "/bin"):
        if extra not in path.split(":"):
            path = f"{path}:{extra}" if path else extra
    LAUNCHD_LOGS.mkdir(parents=True, exist_ok=True)
    job.update({
        "ProgramArguments": [sys.executable, "-m", "autoclipper.cli", "--config", str(config.resolve()), "run"],
        "WorkingDirectory": str(settings.data_dir),
        "EnvironmentVariables": {"PATH": path,
                                 "HF_HOME": os.environ.get("HF_HOME", str(settings.data_dir / "hf"))},
        "StandardOutPath": str(LAUNCHD_LOGS / "autoclipper.out.log"),
        "StandardErrorPath": str(LAUNCHD_LOGS / "autoclipper.err.log"),
    })
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    out = LAUNCH_AGENTS / f"{LABEL}.plist"
    with out.open("wb") as f:
        plistlib.dump(job, f)
    return out


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoclipper")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "start", "stop", "status"):
        commands.add_parser(name)
    for name, what in (("resume", "lift the upload pause of this profile (default: every profile)"),
                       ("login", "open Chrome on this profile's Studio sign-in (default: youtube_profile)")):
        commands.add_parser(name).add_argument("--profile", type=validate_profile, help=what)
    auth = commands.add_parser("auth")
    auth.add_argument("json", type=Path, nargs="?")
    auth.add_argument("--profile", type=validate_profile, default=DEFAULT_PROFILE,
                      help="YouTube Keychain profile to authorize (default: primary)")
    auth.add_argument("--analytics", action="store_true",
                      help="also allow reading YouTube Analytics (watch-through for the learning loop)")
    report = commands.add_parser("report", help="views and likes per presentation variant")
    report.add_argument("--days", type=int, default=7, help="uploads from the last N days (default: 7)")
    learned = commands.add_parser("learn", help="what the pipeline has learned from its results")
    action = learned.add_mutually_exclusive_group()
    action.add_argument("--freeze", action="store_true", help="stop updating; the current values stay in use")
    action.add_argument("--unfreeze", action="store_true", help="resume updating")
    action.add_argument("--reset", action="store_true", help="back to the hand-set values, frozen there")
    action.add_argument("--import", dest="import_db", type=Path, metavar="PATH",
                        help="copy an older run's autoclipper.db into the record")
    instagram_auth = commands.add_parser("instagram-auth")
    instagram_auth.add_argument("token_file", type=Path, help="file containing the Meta Page access token")
    return parser


def _status(config: Path) -> None:
    settings = load(config)
    db = open_db(settings.data_dir / "autoclipper.db")
    legacy = flag(db, PAUSED)                      # shown, never moved: only the daemon's startup migrates it
    now = time.time()
    print("Streams:")
    # configured streams, plus any stream discovery watched in the last hour (its status is newer than that)
    keys = [c.key for c in settings.streams]
    for key, value in db.execute("select key, value from kv where key like 'stream:%' order by key"):
        try:
            recent = now - json.loads(value).get("updated", 0) < 3600
        except (TypeError, ValueError, AttributeError):
            recent = False
        if recent and key[len("stream:"):] not in keys:
            keys.append(key[len("stream:"):])
    for key in keys:
        raw = flag(db, f"stream:{key}")
        try:
            state = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            state = {}
        alive = bool(state.get("capture", False))
        updated = state.get("updated")
        freshness = " stale" if not isinstance(updated, (int, float)) or now - updated > 30 else ""
        chat = state.get("last_chat")
        since_chat = "never" if chat is None else f"{max(0, int(now - chat))}s ago"
        events = state.get("events") or []
        event_count = sum(1 for event in events if isinstance(event, (int, float)) and now - 3600 <= event <= now)
        print(f"  {key}: capture {'alive' if alive else 'down'}{freshness}; chat {since_chat}; events last hour {event_count}")
    for profile in profiles(settings):
        variants = [v for v in settings.caption_variants if profile_for(settings, v) == profile]
        if settings.instagram_account_id and profile in instagram_profiles(settings):
            variants.append("Instagram")
        print(f"YouTube profile: {profile} ({', '.join(variants)})")
        print(f"  Uploads last 24h: {uploads_since(db, now - 86400, profile=profile)}")
        own = paused(db, profile)
        print(f"  Uploads paused: {own or (f'{legacy} (global, moved here at the next start)' if legacy else 'no')}")
    if settings.narrative:
        print("Editing: full story with a Short of at least 30 seconds")
    if settings.instagram_account_id:
        reels = db.execute("select count(*) from outputs where format = 'instagram_reel'"
                           " and state = 'uploaded' and uploaded_at >= ? and json_extract(meta, '$.account_id') = ?",
                           (now - 86400, settings.instagram_account_id)).fetchone()[0]
        print(f"Instagram Reels last 24h: {reels}")
    saved = db.execute("select count(*) from clips where state = 'saved' and uploaded_at >= ?", (now - 86400,)).fetchone()[0]
    print(f"Saved clips last 24h (uploads off): {saved}")
    if settings.landscape:
        waiting = db.execute("select count(*) from outputs where state = 'pending'").fetchone()[0]
        print(f"Landscape companions waiting: {waiting}")
    day = time.strftime("%Y-%m-%d")
    print("LLM calls today:")
    for provider in PROVIDERS:
        print(f"  {provider.name}: {llm_calls(db, provider.name, day)}")
    db.close()


def _report(config: Path, days: int) -> None:
    """Per profile in use, one line per presentation variant: Shorts posted there in the last `days` days, their
    total and median views, total likes, and how many YouTube returned no statistics for (deleted, private, new)."""
    from autoclipper import upload
    settings = load(config)
    db = open_db(settings.data_dir / "autoclipper.db")
    rows = db.execute("select youtube_profile, coalesce(variant, 'none') as variant, video_id from clips"
                      " where video_id is not null and state = 'uploaded' and uploaded_at >= ?"
                      " order by variant", (time.time() - days * 86400,)).fetchall()
    db.close()
    posted = {p: [r for r in rows if r["youtube_profile"] == p] for p in profiles(settings)}
    if not any(posted.values()):
        print(f"No uploads in the last {days} days.")
        return
    print(f"Last {days} days:")
    for profile, mine in posted.items():
        if not mine:
            continue
        try:
            stats = upload.video_stats(upload.client(profile)[0], [r["video_id"] for r in mine])
        except Exception as e:                     # one channel that cannot sign in never hides the others
            print(f"  profile {profile}: {len(mine)} posted; statistics unavailable ({type(e).__name__}: {e})")
            continue
        groups: dict[str, list[str]] = {}
        for r in mine:
            groups.setdefault(r["variant"], []).append(r["video_id"])
        print(f"  profile {profile}:")
        for variant, ids in groups.items():
            known = [stats[i] for i in ids if i in stats]
            parts = [f"{len(ids)} posted"]
            if known:
                views = [v for v, _ in known]
                parts += [f"{sum(views)} views", f"{sum(likes for _, likes in known)} likes",
                          f"median {statistics.median(views):.0f} views"]
            if len(known) < len(ids):
                parts.append(f"{len(ids) - len(known)} without stats")
            print(f"    {variant}: {', '.join(parts)}")


def _learn(config: Path, args) -> None:
    """Show what was learned and why, or freeze, unfreeze, reset or import into the learning record."""
    from autoclipper import jev, learn
    settings = load(config)
    path = learn_path(settings)
    ldb = learn.open_learning(path)
    if args.freeze or args.unfreeze:
        learn.freeze(ldb, args.freeze)
        print("learning frozen; the current values stay in use" if args.freeze else "learning resumed")
    elif args.reset:
        learn.reset(ldb, time.time())
        print("hand-set values restored; learning is frozen (autoclipper learn --unfreeze resumes it)")
    elif args.import_db:
        if not args.import_db.is_file():
            raise SystemExit(f"no such file: {args.import_db}")
        print(f"imported {learn.import_run(ldb, args.import_db)} candidates")
    else:
        if not settings.learn:
            print("learn = false: recording only, hand-set values in use")
        count = lambda where, join="": ldb.execute(f"select count(*) from candidates c {join} where {where}").fetchone()[0]
        got = learn.state(ldb)
        print(f"record: {path}")
        print(f"candidates {count('1')}, editor verdicts {count('c.kept is not null')}, "
              f"posts {count('c.video_id is not null')}, posts with a 24 h result "
              f"{count('s.age_h = 24', 'join snapshots s on s.video_id = c.video_id')}")
        if flag(ldb, "frozen") == "1":
            print("frozen")
        for key, hand in jev.WEIGHTS.items():
            print(f"{key} {got.get('weights', jev.WEIGHTS)[key]:.2f} (hand-set {hand:.2f})")
        if "min_rank" in got:
            print(f"slot minimum {got['min_rank']:.2f} (hand-set {jev.MIN_RANK:.2f})")
            print(f"post at once {got['fast_lane']:.2f} (configured {settings.fast_lane:.2f})")
        else:
            print(f"bars: hand-set (fewer than {learn.BAR_MIN_CANDIDATES} candidates in 24 h)")
        print("editor examples: " + ("learned" if got.get("examples") else "hand-picked"))
        for line in got.get("examples", ()):
            print(f"  {line}")
        print("last changes:")
        for c in learn.changes(ldb):
            print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(c['at']))} {c['what']}: "
                  f"{c['before']} -> {c['after']} ({c['why']})")
    ldb.close()


def _resume(db, settings, profile: str | None) -> None:
    """Lift one profile's pause, or every pause (the old global one first moves into every profile in use)."""
    migrate_pause(db, profiles(settings), settings.youtube_profile)
    if profile is None:
        db.execute("update kv set value = '' where key like ?", (f"{PAUSED}:%",))
    else:
        pause(db, profile, "")


def main(argv=None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "run":
        from autoclipper.run import main as run
        asyncio.run(run(args.config))
    elif args.command == "start":
        target = f"gui/{os.getuid()}/{LABEL}"
        if subprocess.run(["launchctl", "print", target], check=False, capture_output=True).returncode == 0:
            print(f"{LABEL} is already loaded; run `autoclipper stop` first.")      # bootstrap would fail with error 5
            return
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(write_job(args.config))],
                                check=False)
        print(f"launchctl exited with {result.returncode}")
    elif args.command == "stop":
        result = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
        print(f"launchctl exited with {result.returncode}")
    elif args.command == "status":
        _status(args.config)
    elif args.command == "report":
        _report(args.config, args.days)
    elif args.command == "learn":
        _learn(args.config, args)
    elif args.command == "resume":
        settings = load(args.config)
        db = open_db(settings.data_dir / "autoclipper.db")
        _resume(db, settings, args.profile)
        db.close()
        print(f"Uploads resumed on {args.profile or 'every profile'}.")
    elif args.command == "login":
        from autoclipper.studio import launch_login
        settings = load(args.config)
        launch_login(settings.chrome_profile(args.profile or settings.youtube_profile))
    elif args.command == "instagram-auth":
        from .instagram import authorize
        settings = load(args.config)
        if not settings.instagram_account_id:
            raise SystemExit("Set instagram_account_id in the run config first.")
        username = authorize(settings.instagram_account_id, args.token_file.read_text())
        print(f"Instagram authorization saved for @{username}.")
    elif args.command == "auth":
        from autoclipper.upload import auth
        channel = auth(args.json, profile=args.profile, analytics=args.analytics)
        title = channel.get("snippet", {}).get("title", channel["id"])
        print(f"YouTube profile {args.profile!r} authorized for {title} ({channel['id']}).")


if __name__ == "__main__":
    main()
