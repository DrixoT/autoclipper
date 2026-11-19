import types
from pathlib import Path

import pytest

from autoclipper.config import Settings, StreamCfg, disk_ok, load


def test_load_three_streams(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[[stream]]\nplatform="twitch"\nchannel="kaicenat"\n'
                 '[[stream]]\nplatform="youtube"\nchannel="@IShowSpeed"\npre_roll_s=32\n'
                 '[[stream]]\nplatform="kick"\nchannel="adinross"\n')
    s = load(p)
    assert [c.key for c in s.streams] == ["twitch:kaicenat", "youtube:@IShowSpeed", "kick:adinross"]
    assert s.streams[1].pre_roll_s == 32 and s.streams[0].pre_roll_s == 25.0
    assert s.streams[1].url == "https://www.youtube.com/@IShowSpeed/live"

def test_load_defaults_without_a_volume(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text(ONE)
    s = load(p)
    assert s.data_dir == Path.home() / "autoclipper-data"
    assert s.volume is None

def test_disk_ok_without_volume_checks_data_dir_without_mount(tmp_path, monkeypatch):
    checked = []
    monkeypatch.setattr("os.path.ismount", lambda p: pytest.fail("must not check for a mount"))
    monkeypatch.setattr("shutil.disk_usage", lambda p: checked.append(p) or types.SimpleNamespace(free=6 * 2**30))
    s = Settings(streams=(), data_dir=tmp_path)
    assert disk_ok(s) is True
    assert checked == [tmp_path]

def test_load_with_volume_still_rejects_data_dir_outside_it(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text(f'volume="{tmp_path}/volume"\ndata_dir="{tmp_path}/elsewhere"\n' + ONE)
    with pytest.raises(ValueError, match="data_dir"):
        load(p)

@pytest.mark.parametrize("body", [
    ''.join(f'[[stream]]\nplatform="twitch"\nchannel="c{i}"\n' for i in range(6)),   # six distinct streams (limit 5)
    '[[stream]]\nplatform="rumble"\nchannel="a"\n',               # unknown platform
    '[[stream]]\nplatform="twitch"\nchannel="a"\n' * 2,           # duplicate
    '',                                                           # none
])
def test_load_rejects(tmp_path, body):
    p = tmp_path / "s.toml"; p.write_text(body)
    with pytest.raises(ValueError):
        load(p)

def test_disk_ok_false_when_volume_unmounted(tmp_path):
    s = Settings(streams=(), data_dir=tmp_path / "d", volume=tmp_path / "not-a-mount")
    assert disk_ok(s) is False

def test_disk_ok_false_when_full(tmp_path, monkeypatch):
    monkeypatch.setattr("os.path.ismount", lambda p: True)
    monkeypatch.setattr("shutil.disk_usage", lambda p: types.SimpleNamespace(free=1 * 2**30))
    assert disk_ok(Settings(streams=(), data_dir=tmp_path, volume=tmp_path)) is False

ONE = '[[stream]]\nplatform="twitch"\nchannel="a"\n'

@pytest.mark.parametrize("body", [
    'slot_minutes=0\n' + ONE, 'slot_minutes=5\n' + ONE, 'max_uploads_per_day=400\n' + ONE, 'max_uploads_per_day=0\n' + ONE,
    '[[stream]]\nplatform="twitch"\nchannel=123\n', '[[stream]]\nplatform="twitch"\nchannel=""\n',
    '[[stream]]\nplatform="twitch"\nchannel="a"\npre_roll_s="bad"\n', '[[stream]]\nplatform="twitch"\nchannel="a"\npre_roll_s=500\n',
    'data_dir="/tmp/autoclipper"\nvolume="/"\n' + ONE,
    'volume="/tmp/volume"\ndata_dir="/tmp/elsewhere"\n' + ONE])
def test_load_rejects_bad_values(tmp_path, body):
    p = tmp_path / "s.toml"; p.write_text(body)
    with pytest.raises(ValueError):
        load(p)

def test_disk_ok_false_when_usage_check_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("os.path.ismount", lambda p: True)
    def gone(p): raise FileNotFoundError(p)
    monkeypatch.setattr("shutil.disk_usage", gone)
    assert disk_ok(Settings(streams=(), data_dir=tmp_path, volume=tmp_path)) is False

def test_display_name():
    assert StreamCfg("youtube", "@IShowSpeed").display == "IShowSpeed"
    assert StreamCfg("twitch", "kaicenat", name="Kai Cenat").display == "Kai Cenat"

def test_load_rejects_a_handle_as_name(tmp_path):
    p = tmp_path / "s.toml"; p.write_text('[[stream]]\nplatform="twitch"\nchannel="a"\nname="@x"\n')
    with pytest.raises(ValueError):
        load(p)


def test_clips_dir_is_optional_and_must_stay_on_the_volume(tmp_path):
    base = f'volume="{tmp_path}"\ndata_dir="{tmp_path}/data"\n[[stream]]\nplatform="twitch"\nchannel="a"\n'
    (tmp_path / "a.toml").write_text(base)
    assert load(tmp_path / "a.toml").clips_dir is None
    (tmp_path / "b.toml").write_text(f'clips_dir="{tmp_path}/Videos/autoclipper"\n' + base)
    assert load(tmp_path / "b.toml").clips_dir == tmp_path / "Videos" / "autoclipper"
    (tmp_path / "c.toml").write_text('clips_dir="/tmp/elsewhere"\n' + base)
    with pytest.raises(ValueError, match="clips_dir"):
        load(tmp_path / "c.toml")


# --- round 3: five streams and [discover]

def _streams(n):
    return "".join(f'[[stream]]\nplatform="twitch"\nchannel="c{i}"\n' for i in range(n))

def test_five_streams_allowed_six_not(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text(_streams(5)); assert len(load(p).streams) == 5
    p.write_text(_streams(6))
    with pytest.raises(ValueError):
        load(p)

def test_discover_settings_with_pins_and_preferences(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[discover]\ngame = "Grand Theft Auto VI"\n'
                 'pin = ["youtube:@IShowSpeed", {platform = "twitch", channel = "kaicenat", name = "Kai Cenat"}]\n'
                 'prefer = ["kick:xqc", "twitch:xqc", "twitch:jynxzi", "kick:adinross"]\n')
    d = load(p).discover
    assert d.game == "Grand Theft Auto VI" and d.platforms == ("twitch", "kick") and d.language == "en"
    assert (d.min_viewers, d.refresh_minutes) == (500, 5)
    assert [c.key for c in d.pin] == ["youtube:@IShowSpeed", "twitch:kaicenat"] and d.pin[1].display == "Kai Cenat"
    assert [c.key for c in d.prefer] == ["kick:xqc", "twitch:xqc", "twitch:jynxzi", "kick:adinross"]
    assert load(p).streams == ()

@pytest.mark.parametrize("body", [
    '[discover]\ngame = ""\n',
    '[discover]\ngame = "G"\nplatforms = ["youtube"]\n',
    '[discover]\ngame = "G"\npin = ["rumble:x"]\n',
    '[discover]\ngame = "G"\nprefer = ["twitch"]\n',
    '[discover]\ngame = "G"\nmin_viewers = -1\n',
    '[discover]\ngame = "G"\nrefresh_minutes = 0\n',
    '[discover]\ngame = "G"\npin = ["twitch:a", "twitch:b", "twitch:c", "twitch:d", "twitch:e", "twitch:f"]\n',
    '[discover]\ngame = "G"\nsurprise = 1\n',
])
def test_discover_rejects_bad_values(tmp_path, body):
    p = tmp_path / "s.toml"; p.write_text(body)
    with pytest.raises(ValueError):
        load(p)

def test_fast_lane_and_a_higher_daily_cap(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('slot_minutes = 15\nmax_uploads_per_day = 60\nfast_lane = 0.8\nmin_gap_minutes = 5\n' + ONE)
    s = load(p)
    assert (s.slot_minutes, s.max_uploads_per_day, s.fast_lane, s.min_gap_minutes) == (15, 60, 0.8, 5)
    assert (Settings(streams=()).fast_lane, Settings(streams=()).min_gap_minutes) == (0.8, 5)

@pytest.mark.parametrize("body", ['max_uploads_per_day = 101\n', 'fast_lane = 1.5\n', 'fast_lane = "x"\n',
                                  'min_gap_minutes = -1\n'])
def test_fast_lane_rejects_bad_values(tmp_path, body):
    p = tmp_path / "s.toml"; p.write_text(body + ONE)
    with pytest.raises(ValueError):
        load(p)

def test_discover_never_list(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[discover]\ngame = "G"\nnever = ["twitch:someone", "kick:other"]\n')
    assert [c.key for c in load(p).discover.never] == ["twitch:someone", "kick:other"]
    p.write_text('[discover]\ngame = "G"\nnever = ["nonsense"]\n')
    with pytest.raises(ValueError):
        load(p)


def test_the_example_config_loads():
    from pathlib import Path
    s = load(Path(__file__).resolve().parents[1] / "streams.example.toml")
    assert s.discover.game == "Grand Theft Auto VI" and [c.key for c in s.discover.pin][0] == "youtube:@IShowSpeed"
    assert (s.slot_minutes, s.max_uploads_per_day, s.fast_lane, s.min_gap_minutes, s.upload) == (15, 60, 0.8, 5, True)


def test_landscape_is_off_by_default_and_must_be_a_bool(tmp_path):
    p = tmp_path / "s.toml"
    base = f'data_dir="{tmp_path}"\nvolume="{tmp_path}"\n' + ONE
    p.write_text(base); assert load(p).landscape is False
    p.write_text("landscape = true\n" + base); assert load(p).landscape is True
    p.write_text('landscape = "yes"\n' + base)
    with pytest.raises(ValueError, match="landscape"):
        load(p)


def test_story_editing_is_the_default(tmp_path):
    assert Settings(streams=(), data_dir=tmp_path, volume=tmp_path).narrative is True


@pytest.mark.parametrize("value", ["[]", '["neon"]', '["box", "box"]', '"box"'])
def test_caption_variants_are_validated(tmp_path, value):
    (tmp_path / "s.toml").write_text(f'caption_variants = {value}\nvolume="{tmp_path}"\ndata_dir="{tmp_path}/d"\n'
                                     '[[stream]]\nplatform="twitch"\nchannel="a"\n')
    with pytest.raises(ValueError, match="caption_variants"):
        load(tmp_path / "s.toml")


def test_caption_variants_load_as_a_tuple(tmp_path):
    (tmp_path / "s.toml").write_text(f'caption_variants = ["emoji"]\nvolume="{tmp_path}"\ndata_dir="{tmp_path}/d"\n'
                                     '[[stream]]\nplatform="twitch"\nchannel="a"\n')
    assert load(tmp_path / "s.toml").caption_variants == ("emoji",)
    assert Settings(streams=(), data_dir=tmp_path, volume=tmp_path).caption_variants == ("box", "emoji")


# --- per-variant channel routing

def _routed(tmp_path, extra: str):
    p = tmp_path / "s.toml"
    p.write_text(extra + f'\nvolume="{tmp_path}"\ndata_dir="{tmp_path}/d"\n' + ONE)
    return load(p)


def test_without_variant_profiles_every_variant_posts_on_the_youtube_profile(tmp_path):
    from autoclipper.config import instagram_profiles, profile_for, profiles
    s = _routed(tmp_path, 'youtube_profile = "backup"')
    assert s.variant_profiles == {} and profile_for(s, "box") == profile_for(s, "emoji") == "backup"
    assert profiles(s) == ("backup",) and instagram_profiles(s) == ("backup",)


def test_variant_profiles_route_each_variant_to_its_channel(tmp_path):
    from autoclipper.config import instagram_profiles, profile_for, profiles
    s = _routed(tmp_path, 'variant_profiles = { box = "backup", emoji = "primary" }\ninstagram_profiles = ["backup"]')
    assert (profile_for(s, "box"), profile_for(s, "emoji")) == ("backup", "primary")
    assert profiles(s) == ("backup", "primary") and instagram_profiles(s) == ("backup",)
    assert s.chrome_profile("backup") == tmp_path / "d" / "chrome-profiles" / "backup"
    assert s.chrome_profile("primary") == tmp_path / "d" / "chrome-profile"
    s = _routed(tmp_path, 'caption_variants = ["emoji"]\nvariant_profiles = { emoji = "backup" }')
    assert profile_for(s, "emoji") == "backup" and profiles(s) == ("backup",)


@pytest.mark.parametrize("extra", [
    'variant_profiles = { neon = "backup" }',                          # not a variant
    'variant_profiles = { emoji = "backup" }',                         # box, an enabled variant, is not mapped
    'caption_variants = ["box"]\nvariant_profiles = { emoji = "backup" }',   # a variant that is never posted
    'variant_profiles = { box = "Back Up" }',                         # not a profile name
    'variant_profiles = { box = 3 }',
    'variant_profiles = ["backup"]',
])
def test_variant_profiles_are_validated(tmp_path, extra):
    with pytest.raises(ValueError, match="variant_profiles"):
        _routed(tmp_path, extra)


@pytest.mark.parametrize("extra", [
    'instagram_profiles = "backup"',
    'instagram_profiles = ["Back Up"]',
    'instagram_profiles = ["backup", "backup"]',
    'instagram_profiles = ["backup"]',                                 # a profile no variant posts on
    'instagram_account_id = "42"\nvariant_profiles = { box = "backup", emoji = "backup" }',   # default: unused primary
])
def test_instagram_profiles_are_validated(tmp_path, extra):
    with pytest.raises(ValueError, match="instagram_profiles"):
        _routed(tmp_path, extra)


def test_instagram_can_be_off_for_every_profile(tmp_path):
    from autoclipper.config import instagram_profiles
    assert instagram_profiles(_routed(tmp_path, "instagram_profiles = []")) == ()


# --- learning loop

def test_learn_defaults_and_record_path(tmp_path):
    from autoclipper.config import learn_path
    base = f'volume="{tmp_path}"\ndata_dir="{tmp_path}/data"\n' + ONE
    (tmp_path / "a.toml").write_text(base)
    s = load(tmp_path / "a.toml")
    assert s.learn is True and learn_path(s) == tmp_path / "data" / "learning.db"
    (tmp_path / "b.toml").write_text(f'learn_db="{tmp_path}/shared/learning.db"\nlearn=false\n' + base)
    s = load(tmp_path / "b.toml")
    assert s.learn is False and learn_path(s) == tmp_path / "shared" / "learning.db"

@pytest.mark.parametrize("body", ['learn = "yes"\n', 'learn = 1\n', 'learn_db = "/tmp/elsewhere/learning.db"\n'])
def test_learn_rejects_bad_values(tmp_path, body):
    p = tmp_path / "s.toml"; p.write_text(f'volume="{tmp_path}"\ndata_dir="{tmp_path}/data"\n' + body + ONE)
    with pytest.raises(ValueError):
        load(p)
