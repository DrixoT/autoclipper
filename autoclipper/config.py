import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

PLATFORMS = ("twitch", "kick", "youtube")
PRIVACY = ("public", "unlisted", "private")
DEFAULT_PROFILE = "primary"
_PROFILE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
MAX_STREAMS = 5                                    # the owner raised it from 3 on 2026-11-17
CAPTION_VARIANTS = ("box", "emoji")                # box: dark-panel captions; emoji: stroked captions, emoji title
MAX_UPLOADS_PER_DAY = 100                          # YouTube's Video Uploads bucket
DISCOVER_PLATFORMS = ("twitch", "kick")            # YouTube search costs too much quota; pin YouTube instead


@dataclass(frozen=True)
class StreamCfg:
    platform: str
    channel: str
    pre_roll_s: float = 25.0
    name: str = ""                                 # display name for titles and credits

    @property
    def display(self) -> str:
        return self.name or self.channel.lstrip("@")

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.channel}"

    @property
    def url(self) -> str:
        return {"twitch": f"https://twitch.tv/{self.channel}",
                "kick": f"https://kick.com/{self.channel}",
                "youtube": f"https://www.youtube.com/{self.channel}/live"}[self.platform]


@dataclass(frozen=True)
class Discover:
    game: str
    platforms: tuple[str, ...] = DISCOVER_PLATFORMS
    language: str = "en"
    min_viewers: int = 500
    refresh_minutes: int = 5
    pin: tuple[StreamCfg, ...] = ()               # always take a slot while live, whatever they play
    prefer: tuple[StreamCfg, ...] = ()            # take a slot while live, any game, after the pinned ones
    never: tuple[StreamCfg, ...] = ()             # owner-maintained: never watched, even when pinned or preferred


@dataclass(frozen=True)
class Settings:
    streams: tuple[StreamCfg, ...]
    data_dir: Path = Path.home() / "autoclipper-data"
    volume: Path | None = None
    slot_minutes: int = 30
    max_uploads_per_day: int = 40
    privacy: str = "public"
    ffmpeg: str = "/opt/homebrew/bin/ffmpeg"
    upload: bool = True                           # false: everything but YouTube; finished clips are kept as "saved"
    fast_lane: float = 0.8                        # a candidate whose Jev rank (0..1) reaches this is posted at once
    min_gap_minutes: int = 5                      # ...but never sooner than this after the previous upload
    discover: Discover | None = None
    clips_dir: Path | None = None                  # working clips, removed 72 h after they finish; default <data_dir>/clips
    youtube_profile: str = DEFAULT_PROFILE
    instagram_account_id: str | None = None
    narrative: bool = True
    landscape: bool = False                       # also post a 16:9 regular video of each posted clip, a bit longer
    caption_variants: tuple[str, ...] = CAPTION_VARIANTS   # posts alternate between these; one entry turns it off
    variant_profiles: dict[str, str] = field(default_factory=dict)   # variant -> profile; unmapped: youtube_profile
    instagram_profiles: tuple[str, ...] | None = None   # public Shorts become Reels on these; None: youtube_profile
    learn: bool = True                            # false: results are recorded but only the hand-set values are used
    learn_db: Path | None = None                   # the record of candidates and results; default <data_dir>/learning.db

    def chrome_profile(self, profile: str) -> Path:
        """The Chrome profile signed in to Studio for a YouTube profile (the Studio fallback)."""
        if profile == DEFAULT_PROFILE:
            return self.data_dir / "chrome-profile"
        return self.data_dir / "chrome-profiles" / profile


def profile_for(s: Settings, variant: str) -> str:
    """The YouTube profile a clip of this presentation variant is posted on: the one place that decides it."""
    return s.variant_profiles.get(variant, s.youtube_profile)


def profiles(s: Settings) -> tuple[str, ...]:
    """Every YouTube profile a configured variant posts on, in variant order."""
    return tuple(dict.fromkeys(profile_for(s, v) for v in s.caption_variants))


def learn_path(s: Settings) -> Path:
    return s.learn_db or s.data_dir / "learning.db"


def instagram_profiles(s: Settings) -> tuple[str, ...]:
    return (s.youtube_profile,) if s.instagram_profiles is None else s.instagram_profiles


def validate_profile(profile: str) -> str:
    if not isinstance(profile, str) or not _PROFILE_NAME.fullmatch(profile):
        raise ValueError("YouTube profile must start with a lowercase letter and use up to 32 lowercase "
                         "letters, digits, underscores or hyphens")
    return profile


def _stream(entry) -> StreamCfg:
    """A stream from a [[stream]] table, or from a "platform:channel" string in a [discover] list."""
    try:
        if isinstance(entry, str):
            platform, sep, channel = entry.partition(":")
            if not sep:
                raise ValueError(f"expected platform:channel, got {entry!r}")
            return StreamCfg(platform, channel)
        return StreamCfg(**entry)
    except TypeError as e:
        raise ValueError(f"bad stream entry {entry!r}: {e}") from None


def _check_streams(streams: tuple[StreamCfg, ...]) -> None:
    for c in streams:
        if c.platform not in PLATFORMS:
            raise ValueError(f"unknown platform {c.platform!r}; use one of {PLATFORMS}")
        if not isinstance(c.channel, str) or not c.channel.strip():
            raise ValueError("channel must be a non-empty string")
        if not _num(c.pre_roll_s) or not 0 <= c.pre_roll_s <= 120:
            raise ValueError("pre_roll_s must be a number from 0 to 120")
        if not isinstance(c.name, str) or any(ch in c.name for ch in "@<>"):
            raise ValueError("name must be a string without @, < or >")
    if len({c.key for c in streams}) != len(streams):
        raise ValueError("duplicate stream")


def _discover(raw: dict) -> Discover:
    raw = dict(raw)
    try:
        d = Discover(**{**raw, "pin": tuple(_stream(e) for e in raw.get("pin", ())),
                        "prefer": tuple(_stream(e) for e in raw.get("prefer", ())),
                        "never": tuple(_stream(e) for e in raw.get("never", ())),
                        "platforms": tuple(raw.get("platforms", DISCOVER_PLATFORMS))})
    except TypeError as e:
        raise ValueError(f"bad [discover]: {e}") from None
    if not isinstance(d.game, str) or not d.game.strip():
        raise ValueError("[discover] game must be a non-empty string")
    if not d.platforms or not set(d.platforms) <= set(DISCOVER_PLATFORMS):
        raise ValueError(f"[discover] platforms must be some of {DISCOVER_PLATFORMS}")
    if not isinstance(d.language, str) or not _int(d.min_viewers) or d.min_viewers < 0:
        raise ValueError("[discover] language must be a string and min_viewers a whole number >= 0")
    if not _int(d.refresh_minutes) or not 1 <= d.refresh_minutes <= 60:
        raise ValueError("[discover] refresh_minutes must be an integer from 1 to 60")
    if len(d.pin) > MAX_STREAMS:
        raise ValueError(f"[discover] at most {MAX_STREAMS} pinned streams")
    _check_streams(d.pin)
    _check_streams(d.prefer)
    _check_streams(d.never)
    return d


def load(path: Path) -> Settings:
    raw = tomllib.loads(Path(path).read_text())
    streams = tuple(_stream(t) for t in raw.pop("stream", []))
    if "discover" in raw:
        raw["discover"] = _discover(raw["discover"])
    elif not streams:
        raise ValueError("need at least one [[stream]] or a [discover] section")
    if len(streams) > MAX_STREAMS:
        raise ValueError(f"need at most {MAX_STREAMS} streams, got {len(streams)}")
    _check_streams(streams)
    unknown = raw.keys() - {f.name for f in fields(Settings)}
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    for k in ("data_dir", "volume", "clips_dir", "learn_db"):
        if k in raw and raw[k] is not None:
            raw[k] = Path(raw[k]).expanduser()
    if "caption_variants" in raw:
        variants = raw["caption_variants"]
        if (not isinstance(variants, list) or not variants or len(set(variants)) != len(variants)
                or not set(variants) <= set(CAPTION_VARIANTS)):
            raise ValueError(f"caption_variants must be a non-empty list of distinct names from {CAPTION_VARIANTS}")
        raw["caption_variants"] = tuple(variants)
    if "instagram_profiles" in raw:
        names = raw["instagram_profiles"]
        if not isinstance(names, list) or len(set(names)) != len(names):
            raise ValueError("instagram_profiles must be a list of distinct YouTube profiles")
        raw["instagram_profiles"] = tuple(names)
    s = Settings(streams=streams, **raw)
    validate_profile(s.youtube_profile)
    routes = s.variant_profiles
    if not isinstance(routes, dict) or (routes and routes.keys() != set(s.caption_variants)):
        raise ValueError(f"variant_profiles must map every one of caption_variants {s.caption_variants} "
                         "to a YouTube profile")
    for name in routes.values():
        _profile("variant_profiles", name)
    for name in s.instagram_profiles or ():
        _profile("instagram_profiles", name)
        if name not in profiles(s):
            raise ValueError(f"instagram_profiles: no variant posts on {name!r}; profiles in use: {profiles(s)}")
    if s.instagram_account_id and s.instagram_profiles is None and s.youtube_profile not in profiles(s):
        raise ValueError(f"instagram_profiles: Reels default to youtube_profile {s.youtube_profile!r}, which no variant "
                         f"posts on; set instagram_profiles to some of {profiles(s)} (or [] for no Reels)")
    if s.privacy not in PRIVACY:
        raise ValueError(f"privacy must be one of {PRIVACY}")
    if not _int(s.slot_minutes) or not 10 <= s.slot_minutes <= 240:
        raise ValueError("slot_minutes must be an integer from 10 to 240")
    if not _int(s.max_uploads_per_day) or not 1 <= s.max_uploads_per_day <= MAX_UPLOADS_PER_DAY:
        raise ValueError(f"max_uploads_per_day must be an integer from 1 to {MAX_UPLOADS_PER_DAY}")
    if not isinstance(s.upload, bool):
        raise ValueError("upload must be true or false")
    if s.instagram_account_id is not None and (
        not isinstance(s.instagram_account_id, str) or not re.fullmatch(r"[0-9]+", s.instagram_account_id)
    ):
        raise ValueError("instagram_account_id must be a string containing only digits")
    if not isinstance(s.narrative, bool):
        raise ValueError("narrative must be true or false")
    if not isinstance(s.landscape, bool):
        raise ValueError("landscape must be true or false")
    if not isinstance(s.learn, bool):
        raise ValueError("learn must be true or false")
    if not _num(s.fast_lane) or not 0 <= s.fast_lane <= 1:
        raise ValueError("fast_lane must be a number from 0 to 1 (1 turns it off in practice)")
    if not _int(s.min_gap_minutes) or not 0 <= s.min_gap_minutes <= 120:
        raise ValueError("min_gap_minutes must be an integer from 0 to 120")
    if s.volume is not None:
        volume, data = os.path.normpath(s.volume), os.path.normpath(s.data_dir)
        if volume == "/" or not Path(data).is_relative_to(volume):
            raise ValueError("volume must not be / and data_dir must be inside volume")
        if s.clips_dir is not None and not Path(os.path.normpath(s.clips_dir)).is_relative_to(volume):
            raise ValueError("clips_dir must be inside volume")
        if s.learn_db is not None and not Path(os.path.normpath(s.learn_db)).is_relative_to(volume):
            raise ValueError("learn_db must be inside volume")
    return s


def _profile(setting: str, name) -> None:
    try:
        validate_profile(name)
    except ValueError as e:
        raise ValueError(f"{setting}: {e}") from None


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def disk_ok(s: Settings, min_free_gb: float = 5.0) -> bool:
    try:
        if s.volume is None:
            return shutil.disk_usage(s.data_dir).free >= min_free_gb * 2**30
        return os.path.ismount(s.volume) and shutil.disk_usage(s.volume).free >= min_free_gb * 2**30
    except OSError:                                # volume vanished between the two checks
        return False
