import json
import subprocess

import pytest

from autoclipper.discover import Helix, Live, kick_streams, probe, twitch_credentials


def test_helix_token_game_cache_and_stream_mapping():
    calls = []

    def http(method, url, headers, body=None, timeout=20):
        calls.append((method, url, headers, body))
        if url.endswith("/oauth2/token"):
            return 200, {"access_token": "token"}
        if "/games?" in url:
            return 200, {"data": [{"id": "42"}]}
        return 200, {"data": [{"user_login": "SomeLogin", "user_name": "Some Name",
                                "viewer_count": 321, "game_name": "Example Game"}]}

    helix = Helix("client", "secret", http=http)
    assert helix.token() == helix.token() == "token"
    assert helix.game_id("Example Game") == helix.game_id("Example Game") == "42"
    streams = helix.streams("42", "en", first=25)

    assert streams == [Live("twitch", "somelogin", "Some Name", 321, "Example Game")]
    game_calls = [call for call in calls if "/games?" in call[1]]
    assert len(game_calls) == 1
    stream_call = next(call for call in calls if "/streams?" in call[1])
    assert "language=en" in stream_call[1] and "first=25" in stream_call[1]
    assert stream_call[2]["Authorization"] == "Bearer token"
    assert sum(call[1].endswith("/oauth2/token") for call in calls) == 1


def test_helix_streams_retries_401_once_then_errors():
    calls = []

    def http(method, url, headers, body=None, timeout=20):
        calls.append((url, headers))
        if url.endswith("/oauth2/token"):
            return 200, {"access_token": f"token-{sum(u.endswith('/oauth2/token') for u, _ in calls)}"}
        if sum("/streams?" in u for u, _ in calls) == 1:
            return 401, {}
        return 200, {"data": []}

    helix = Helix("client", "secret", http=http)
    assert helix.streams("42", "en") == []
    assert [h["Authorization"] for u, h in calls if "/streams?" in u] == ["Bearer token-1", "Bearer token-2"]

    def always_401(method, url, headers, body=None, timeout=20):
        return (200, {"access_token": "token"}) if url.endswith("/oauth2/token") else (401, {})

    with pytest.raises(RuntimeError, match="401"):
        Helix("client", "secret", http=always_401).streams("42", "en")


@pytest.mark.parametrize("missing", ["twitch_client_id", "twitch_client_secret"])
def test_twitch_credentials_missing_entry(monkeypatch, missing):
    monkeypatch.setattr("autoclipper.discover.keyring.get_password",
                        lambda service, key: None if key == missing else "present")
    assert twitch_credentials() is None


def test_kick_streams_maps_and_filters_language():
    # Trimmed public Kick response shape from the GTA V livestream endpoint.
    category = {"id": 8, "name": "Grand Theft Auto V (GTA)", "slug": "grand-theft-auto-v"}
    response = {"current_page": 1, "next_page_url": None, "data": [
        {"language": "English", "viewer_count": 321,
         "channel": {"slug": "eltioedyoficial", "user": {"username": "EltioedyOficial"}},
         "categories": [category]},
        {"language": "Spanish", "viewer_count": 88,
         "channel": {"slug": "canalpublico", "user": {"username": "CanalPublico"}},
         "categories": [category]},
        {"language": "English", "viewer_count": 50,
         "channel": {"slug": "other", "user": {"username": "Other"}},
         "categories": [{"id": 15, "name": "Just Chatting"}]},
    ]}
    calls = []

    def http(method, url, headers, body=None, timeout=20):
        calls.append((method, url, headers))
        if "/api/search?" in url:
            return 200, {"categories": [category]}
        return 200, response

    assert kick_streams("Grand Theft Auto V", "en", http=http) == [
        Live("kick", "eltioedyoficial", "EltioedyOficial", 321, "Grand Theft Auto V (GTA)")]
    assert len(calls) == 2
    assert all(call[2]["Accept"] == "application/json" for call in calls)
    assert "subcategory=grand-theft-auto-v" in calls[1][1] and "sort=desc" in calls[1][1]   # filter by slug, biggest first


def test_probe_live_error_timeout_and_bad_json():
    def fake_run(stdout):
        return lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    assert probe("https://twitch.tv/example", run=fake_run(
        json.dumps({"streams": {"720p": {}}, "metadata": {"category": "Example Game"}}))) == (True, "Example Game")
    assert probe("url", run=fake_run('{"error":"offline"}')) == (False, None)

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 60)

    assert probe("url", run=timeout) == (False, None)
    assert probe("url", run=fake_run("not json")) == (False, None)


# --- only streamers who allow clipping: a clip in the last 7 days

def test_twitch_recent_clip_check(monkeypatch):
    import autoclipper.discover as d
    seen = []
    def http(method, url, headers, body=None, timeout=20):
        seen.append(url)
        if url.endswith("/oauth2/token"):
            return 200, {"access_token": "token"}
        if "/streams?" in url:
            return 200, {"data": [{"user_id": "77", "user_login": "a", "user_name": "A", "viewer_count": 9, "game_name": "G"}]}
        return 200, {"data": [{"id": "clip"}]} if "broadcaster_id=77" in url else {"data": []}
    monkeypatch.setattr(d.time, "time", lambda: 1_800_000_000.0)
    h = Helix("client", "secret", http=http)
    live = h.streams("1", "en")[0]
    assert live.id == "77" and h.recent_clip(live.id) is True and h.recent_clip("78") is False
    url = next(u for u in seen if "/clips?" in u and "77" in u)
    assert "started_at=2027-01-08T08%3A00%3A00Z" in url and "first=1" in url      # now minus 7 days, RFC 3339

def test_kick_recent_clip_check():
    from autoclipper.discover import kick_recent_clip
    seen = []
    def http(method, url, headers, body=None, timeout=20):
        seen.append((url, headers))
        return (200, {"clips": [{"id": "x"}], "nextCursor": None}) if "/xqc/" in url else (200, {"clips": []})
    assert kick_recent_clip("xqc", http=http) is True and kick_recent_clip("quiet", http=http) is False
    assert seen[0][0] == "https://kick.com/api/v2/channels/xqc/clips?sort=date&time=week"
    assert "Mozilla" in seen[0][1]["User-Agent"]


def test_a_missing_twitch_game_is_asked_again_after_ten_minutes(monkeypatch):
    import autoclipper.discover as d
    clock, asked = [1000.0], []
    monkeypatch.setattr(d.time, "time", lambda: clock[0])
    def http(method, url, headers, body=None, timeout=20):
        if url.endswith("/oauth2/token"):
            return 200, {"access_token": "token"}
        asked.append(url)
        return 200, {"data": [{"id": "1867279146"}]} if clock[0] > 1500 else {"data": []}
    h = Helix("client", "secret", http=http)
    assert h.game_id("Grand Theft Auto VI") is None and h.game_id("Grand Theft Auto VI") is None and len(asked) == 1
    clock[0] = 1700.0
    assert h.game_id("Grand Theft Auto VI") == "1867279146" and len(asked) == 2
    clock[0] = 9999.0
    assert h.game_id("Grand Theft Auto VI") == "1867279146" and len(asked) == 2          # found ids stay


def test_helix_category_of_one_live_channel():
    asked = []
    def http(method, url, headers, body=None, timeout=20):
        asked.append(url)
        if url.endswith("/oauth2/token"):
            return 200, {"access_token": "token"}
        if "user_login=tfue" in url:
            return 200, {"data": [{"user_login": "tfue", "game_name": "ROB RUN"}]}
        return 200, {"data": []}                                   # offline
    helix = Helix("id", "secret", http=http)
    assert helix.category("tfue") == "ROB RUN" and helix.category("gone") is None
    assert any("/streams?user_login=tfue" in u for u in asked)


def test_kick_category_of_one_live_channel():
    from autoclipper.discover import kick_category
    def http(method, url, headers, body=None, timeout=20):
        assert url == "https://kick.com/api/v2/channels/adinross" and "Mozilla" in headers["User-Agent"]
        return 200, {"livestream": {"is_live": True, "categories": [{"name": "Grand Theft Auto VI (GTA)"}]}}
    assert kick_category("adinross", http=http) == "Grand Theft Auto VI (GTA)"
    assert kick_category("x", http=lambda *a, **k: (200, {"livestream": None})) is None
    with pytest.raises(RuntimeError):
        kick_category("x", http=lambda *a, **k: (403, {}))


def test_live_category_tells_a_failed_probe_from_a_stream_with_no_game():
    from autoclipper.discover import ProbeFailed, live_category
    def answer(code=0, out="", exc=None):
        def run(argv, **kw):
            if exc: raise exc
            return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
        return run
    ok = json.dumps({"streams": {"best": {}}, "metadata": {"category": "Minecraft"}})
    assert live_category("u", run=answer(out=ok)) == "Minecraft"
    none = json.dumps({"streams": {"best": {}}, "metadata": {"category": None}})
    assert live_category("u", run=answer(out=none)) is None                    # succeeded: no game
    for failed in (answer(code=1, out='{"error": "No playable streams found"}'), answer(out="not json"),
                   answer(out='{"error": "boom"}'), answer(exc=subprocess.TimeoutExpired("streamlink", 60)),
                   answer(exc=OSError("no streamlink"))):
        with pytest.raises(ProbeFailed):
            live_category("u", run=failed)
