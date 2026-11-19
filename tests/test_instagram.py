import json
import threading

from autoclipper import instagram
from autoclipper.store import add_clip, get_output, open_db, pause, set_state, uploads_since


def test_direct_upload_uses_the_official_hosts_and_cover_offset(tmp_path):
    calls = []
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")

    def request(method, url, token, **kwargs):
        payload = kwargs.get("data")
        if url.startswith(instagram.UPLOAD):
            assert payload.read() == b"video"
            assert kwargs["auth"] == "OAuth"
            assert kwargs["headers"]["Content-Length"] == "5"
        elif payload:
            payload = json.loads(payload)
        calls.append((method, url, payload))
        return {
            1: {"id": "77", "uri": "https://unrelated.invalid/"},
            2: {"success": True},
            3: {"status_code": "FINISHED"},
            4: {"id": "88"},
        }[len(calls)]

    client = instagram.Instagram("42", "token", request=request)
    container = client.stage_reel(video, "Caption and source", 930)
    assert client.status(container) == "FINISHED"
    assert client.publish(container) == "88"
    assert calls[0] == (
        "POST", f"{instagram.GRAPH}/42/media",
        {"media_type": "REELS", "upload_type": "resumable", "caption": "Caption and source", "thumb_offset": 930},
    )
    assert calls[1][1] == f"{instagram.UPLOAD}/77"
    assert calls[3] == ("POST", f"{instagram.GRAPH}/42/media_publish", {"creation_id": "77"})


def _queued(tmp_path):
    db = open_db(tmp_path / "test.db")
    clip_id = add_clip(db, "twitch:name", 10, 20, 3, "funny", [], "/raw", 0)
    instagram.queue(db, clip_id, "backup", "42", tmp_path / "short.mp4", tmp_path / "thumb.jpg",
                    "A title", "A description", "Clipped from Name: https://twitch.tv/name", 350)
    return db, clip_id


def test_reel_is_persisted_then_published_once_after_its_public_short(tmp_path, monkeypatch):
    db, clip_id = _queued(tmp_path)
    steps = []
    monkeypatch.setattr(instagram.keyring, "get_password", lambda service, key: "token")
    monkeypatch.setattr(instagram.Instagram, "stage_reel",
                        lambda client, path, caption, cover: steps.append(("staged", caption, cover)) or "77")
    monkeypatch.setattr(instagram.Instagram, "status", lambda client, container: "FINISHED")
    monkeypatch.setattr(instagram.Instagram, "publish", lambda client, container: steps.append(("published", container)) or "88")
    stop = threading.Event()
    assert instagram.advance(db, "backup", "42", stop) is None and not steps
    set_state(db, clip_id, "uploaded", video_id="yt", uploaded_at=30, youtube_profile="backup", privacy="public")
    assert instagram.advance(db, "backup", "43", stop) is None and not steps
    assert instagram.advance(db, "backup", "42", stop) is None
    assert json.loads(get_output(db, clip_id, instagram.FORMAT)["meta"])["container"] == "77"
    pause(db, "backup", "editor-update")
    assert instagram.advance(db, "backup", "42", stop) is None
    pause(db, "backup", "")
    assert instagram.advance(db, "backup", "42", stop) == "88"
    assert instagram.advance(db, "backup", "42", stop) is None
    assert get_output(db, clip_id, instagram.FORMAT)["video_id"] == "88"
    assert [step[0] for step in steps] == ["staged", "published"]
    assert "https://twitch.tv/name" in steps[0][1]
    assert uploads_since(db, 0) == 1


def test_a_reel_with_an_uncertain_publish_is_not_retried(tmp_path, monkeypatch):
    db, clip_id = _queued(tmp_path)
    set_state(db, clip_id, "uploaded", video_id="yt", uploaded_at=30, youtube_profile="backup", privacy="public")
    monkeypatch.setattr(instagram.keyring, "get_password", lambda *args: "token")
    monkeypatch.setattr(instagram.Instagram, "stage_reel", lambda *args: "77")
    monkeypatch.setattr(instagram.Instagram, "status", lambda *args: "FINISHED")
    publishes = []

    def timed_out(*args):
        publishes.append(1)
        raise instagram.InstagramError("network timeout", retryable=True)

    monkeypatch.setattr(instagram.Instagram, "publish", timed_out)
    stop = threading.Event()
    for _ in range(3):
        instagram.advance(db, "backup", "42", stop)
    assert publishes == [1]
    out = get_output(db, clip_id, instagram.FORMAT)
    assert out["state"] == "failed" and "not confirmed" in out["error"]


def test_a_paused_channel_holds_only_its_own_reels(tmp_path, monkeypatch):
    from autoclipper.store import pause
    db, clip_id = _queued(tmp_path)
    set_state(db, clip_id, "uploaded", video_id="yt", uploaded_at=30, youtube_profile="backup", privacy="public")
    monkeypatch.setattr(instagram.keyring, "get_password", lambda *args: "token")
    monkeypatch.setattr(instagram.Instagram, "stage_reel", lambda *args: "77")
    stop = threading.Event()
    pause(db, "backup", "limit:9")
    assert instagram.advance(db, "backup", "42", stop) is None and "container" not in get_output(db, clip_id, instagram.FORMAT)["meta"]
    pause(db, "backup", ""); pause(db, "primary", "auth")
    assert instagram.advance(db, "backup", "42", stop) is None
    assert json.loads(get_output(db, clip_id, instagram.FORMAT)["meta"])["container"] == "77"


def test_a_reel_waits_unless_its_short_was_uploaded_public(tmp_path, monkeypatch):
    db, clip_id = _queued(tmp_path)
    set_state(db, clip_id, "uploaded", video_id="yt", uploaded_at=30, youtube_profile="backup", privacy="unlisted")
    monkeypatch.setattr(instagram.keyring, "get_password", lambda *args: "token")
    staged = []
    monkeypatch.setattr(instagram.Instagram, "stage_reel", lambda *args: staged.append(1) or "77")
    assert instagram.advance(db, "backup", "42", threading.Event()) is None and staged == []
    set_state(db, clip_id, "uploaded", privacy="public")
    instagram.advance(db, "backup", "42", threading.Event())
    assert staged == [1]
