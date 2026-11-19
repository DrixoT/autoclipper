import json

import httplib2
import pytest
from unittest.mock import MagicMock
from googleapiclient.errors import HttpError

from autoclipper.meta import Meta
from autoclipper.upload import (QuotaExhausted, UploadLimit, client, set_thumbnail, strike_check, upload_short,
                                video_stats, wait_processed)


def http_error(reason, status=403):
    body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    return HttpError(resp=httplib2.Response({"status": status}), content=body)


META = Meta("Kai loses it", "HE DID WHAT", "Kai reads the order out loud.", ("kai", "funny"), 1, "luna")


def test_upload_body_and_return(tmp_path):
    v = tmp_path / "v.mp4"; v.write_bytes(b"0")
    yt = MagicMock(); yt.videos().insert.return_value.next_chunk.return_value = (
        None, {"id": "abc", "status": {"privacyStatus": "public"}})
    assert upload_short(yt, v, META, "Clipped from Kai's live stream: https://twitch.tv/kaicenat", "public") == (
        "abc", "public")                                   # the privacy YouTube says it gave the video
    body = yt.videos().insert.call_args.kwargs["body"]
    assert body["snippet"]["title"] == "Kai loses it" and body["snippet"]["categoryId"] == "20"
    assert body["snippet"]["description"].endswith("https://twitch.tv/kaicenat\n#shorts")
    assert body["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}


@pytest.mark.parametrize("reason,exc", [("uploadLimitExceeded", UploadLimit),
                                        ("quotaExceeded", QuotaExhausted), ("dailyLimitExceeded", QuotaExhausted)])
def test_upload_maps_limit_errors(tmp_path, reason, exc):
    v = tmp_path / "v.mp4"; v.write_bytes(b"0")
    yt = MagicMock(); yt.videos().insert.return_value.next_chunk.side_effect = http_error(reason)
    with pytest.raises(exc):
        upload_short(yt, v, META, "c", "public")


def test_set_thumbnail_false_when_refused(tmp_path):
    j = tmp_path / "t.jpg"; j.write_bytes(b"0")
    yt = MagicMock(); yt.thumbnails().set.return_value.execute.side_effect = http_error("forbidden")
    assert set_thumbnail(yt, "abc", j) is False


def test_strike_check_flags_rejected_and_missing():
    yt = MagicMock()
    yt.videos().list.return_value.execute.return_value = {"items": [
        {"id": "a", "status": {"uploadStatus": "processed"}},
        {"id": "b", "status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}}]}
    assert strike_check(yt, ["a", "b", "c"]) == ["b", "c"]

def test_upload_gives_up_when_nothing_moves(tmp_path):
    v = tmp_path / "v.mp4"; v.write_bytes(b"0")
    yt = MagicMock(); req = yt.videos().insert.return_value
    req.resumable_progress = 0; req.next_chunk.return_value = (None, None)
    with pytest.raises(RuntimeError):
        upload_short(yt, v, META, "c", "public")
    assert req.next_chunk.call_count <= 10 and yt.videos().insert.call_count == 1

def test_set_thumbnail_reraises_server_errors(tmp_path):
    j = tmp_path / "t.jpg"; j.write_bytes(b"0")
    yt = MagicMock(); yt.thumbnails().set.return_value.execute.side_effect = http_error("backendError", status=500)
    with pytest.raises(HttpError):
        set_thumbnail(yt, "abc", j)

def test_client_explains_missing_login(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda service, name: None)
    with pytest.raises(RuntimeError, match="autoclipper auth"):
        client()


def test_client_fails_early_when_the_token_cannot_refresh(monkeypatch):
    import google.auth.exceptions
    secrets = {"client_config": json.dumps({"installed": {"client_id": "c", "client_secret": "s",
                                                          "token_uri": "https://oauth2.example/token"}}),
               "refresh_token": "r"}
    monkeypatch.setattr("keyring.get_password", lambda service, name: secrets[name])
    def refused(self, request): raise google.auth.exceptions.RefreshError("invalid_grant")
    monkeypatch.setattr("autoclipper.upload.Credentials.refresh", refused)
    with pytest.raises(google.auth.exceptions.RefreshError):
        client()


@pytest.mark.parametrize("answer", [{"items": []}, "signup"])
def test_client_refuses_an_account_without_a_channel(monkeypatch, answer):
    secrets = {"client_config": json.dumps({"installed": {"client_id": "c", "client_secret": "s",
                                                          "token_uri": "https://oauth2.example/token"}}),
               "refresh_token": "r"}
    monkeypatch.setattr("keyring.get_password", lambda service, name: secrets[name])
    monkeypatch.setattr("autoclipper.upload.Credentials.refresh", lambda self, request: None)
    yt = MagicMock()
    if answer == "signup":
        yt.channels().list.return_value.execute.side_effect = http_error("youtubeSignupRequired", status=401)
    else:
        yt.channels().list.return_value.execute.return_value = answer
    monkeypatch.setattr("autoclipper.upload.build", lambda *a, **k: yt)
    with pytest.raises(RuntimeError, match="no YouTube channel"):
        client()


def _yt(*pages):
    yt = MagicMock(); yt.videos().list.return_value.execute.side_effect = list(pages); return yt

def test_wait_processed_polls_until_youtube_is_done():
    slept = []
    yt = _yt({"items": [{"processingDetails": {"processingStatus": "processing"}, "status": {"uploadStatus": "uploaded"}}]},
             {"items": [{"processingDetails": {"processingStatus": "succeeded"}, "status": {"uploadStatus": "processed"}}]})
    assert wait_processed(yt, "v", sleep=slept.append, now=lambda: 0.0) is True and slept == [15]

def test_wait_processed_gives_up_on_rejection_and_on_timeout():
    yt = _yt({"items": [{"processingDetails": {"processingStatus": "processing"}, "status": {"uploadStatus": "rejected"}}]})
    assert wait_processed(yt, "v", sleep=lambda s: None, now=lambda: 0.0) is False
    clock = iter([0.0, 700.0])
    yt = _yt({"items": []})
    assert wait_processed(yt, "v", sleep=lambda s: None, now=lambda: next(clock)) is False


def test_named_auth_keeps_primary_and_pins_backup_channel(tmp_path, monkeypatch):
    from autoclipper import upload

    client_config = {"installed": {"client_id": "backup-client", "client_secret": "secret",
                                   "token_uri": "https://oauth2.example/token"}}
    client_json = tmp_path / "client.json"
    client_json.write_text(json.dumps(client_config))
    keychain = {"client_config": "primary-config", "refresh_token": "primary-token"}
    monkeypatch.setattr(upload.keyring, "get_password", lambda service, key: keychain.get(key))
    monkeypatch.setattr(upload.keyring, "set_password", lambda service, key, value: keychain.update({key: value}))

    flow = MagicMock()
    flow.run_local_server.return_value.refresh_token = "backup-token"
    monkeypatch.setattr(upload.InstalledAppFlow, "from_client_config", lambda *args, **kwargs: flow)
    yt = MagicMock()
    channels = yt.channels().list.return_value.execute
    channels.return_value = {"items": [{"id": "backup-channel", "snippet": {"title": "Backup"}}]}
    monkeypatch.setattr(upload, "build", lambda *args, **kwargs: yt)
    monkeypatch.setattr(upload.Credentials, "refresh", lambda self, request: None)

    assert upload.auth(client_json, profile="backup")["id"] == "backup-channel"
    assert keychain["client_config"] == "primary-config"
    assert keychain["refresh_token"] == "primary-token"
    assert json.loads(keychain["youtube:backup"])["refresh_token"] == "backup-token"
    assert upload.client("backup") == (yt, "backup-channel")

    channels.return_value = {"items": [{"id": "wrong-channel"}]}
    with pytest.raises(RuntimeError, match="different channel"):
        upload.client("backup")
    with pytest.raises(RuntimeError, match="already linked"):
        upload.auth(profile="backup")
    assert json.loads(keychain["youtube:backup"])["channel_id"] == "backup-channel"


def test_a_regular_video_has_no_shorts_marker(tmp_path):
    v = tmp_path / "v.mp4"; v.write_bytes(b"0")
    yt = MagicMock(); yt.videos().insert.return_value.next_chunk.return_value = (None, {"id": "abc"})
    upload_short(yt, v, META, "Clipped from Kai's live stream: https://twitch.tv/kaicenat", "unlisted", short=False)
    body = yt.videos().insert.call_args.kwargs["body"]
    assert body["snippet"]["description"] == ("Kai reads the order out loud.\n\n"
                                              "Clipped from Kai's live stream: https://twitch.tv/kaicenat")
    assert "#shorts" not in body["snippet"]["description"] and body["status"]["privacyStatus"] == "unlisted"


def test_video_stats_batches_and_skips_missing():
    yt = MagicMock()
    yt.videos().list.return_value.execute.side_effect = [
        {"items": [{"id": "a", "statistics": {"viewCount": "120", "likeCount": "7"}},
                   {"id": "b", "statistics": {"viewCount": "3"}}]},          # likes hidden: counted as 0
        {"items": []}]
    yt.videos().list.reset_mock()
    got = video_stats(yt, ["a", "b"] + [f"x{i}" for i in range(50)])
    assert got == {"a": (120, 7), "b": (3, 0)} and yt.videos().list.call_count == 2
    first, second = (c.kwargs for c in yt.videos().list.call_args_list)
    assert first["part"] == "statistics" and len(first["id"].split(",")) == 50 and second["id"] == "x48,x49"
    assert video_stats(yt, []) == {}


def _signed_in(monkeypatch, yt):
    secrets = {"client_config": json.dumps({"installed": {"client_id": "c", "client_secret": "s",
                                                          "token_uri": "https://oauth2.example/token"}}),
               "refresh_token": "r"}
    monkeypatch.setattr("keyring.get_password", lambda service, name: secrets[name])
    monkeypatch.setattr("autoclipper.upload.Credentials.refresh", lambda self, request: None)
    monkeypatch.setattr("autoclipper.upload.build", lambda *a, **k: yt)


def test_client_keeps_the_channel_it_verified_for_the_studio_fallback(monkeypatch):
    from autoclipper import upload
    monkeypatch.setattr(upload, "CHANNELS", {})
    yt = MagicMock(); yt.channels().list.return_value.execute.return_value = {"items": [{"id": "UC-b"}]}
    _signed_in(monkeypatch, yt)
    assert upload.verified_channel("primary") is None
    assert client() == (yt, "UC-b")                     # the channel this client was verified with, with it
    assert upload.verified_channel("primary") == "UC-b"


@pytest.mark.parametrize("reason", ["quotaExceeded", "dailyLimitExceeded"])
def test_a_quota_error_while_signing_in_is_quota_not_a_sign_in_failure(monkeypatch, reason):
    yt = MagicMock(); yt.channels().list.return_value.execute.side_effect = http_error(reason, status=403)
    _signed_in(monkeypatch, yt)
    with pytest.raises(QuotaExhausted):
        client()


def test_an_upload_reports_no_privacy_when_youtube_does_not_say(tmp_path):
    v = tmp_path / "v.mp4"; v.write_bytes(b"0")
    yt = MagicMock(); yt.videos().insert.return_value.next_chunk.return_value = (None, {"id": "abc"})
    assert upload_short(yt, v, META, "c", "public") == ("abc", None)


def test_the_manual_upload_tool_uses_the_video_id(tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("upload_tool", Path(__file__).parents[1] / "tools" / "upload_test.py")
    tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
    yt = MagicMock()
    yt.videos().list.return_value.execute.return_value = {"items": [{
        "status": {"uploadStatus": "processed", "privacyStatus": "private"},
        "processingDetails": {"processingStatus": "succeeded"}, "contentDetails": {"duration": "PT10S"},
        "snippet": {"title": "t", "thumbnails": {}}}]}
    monkeypatch.setattr(tool, "client", lambda: (yt, "UC-test"))
    monkeypatch.setattr(tool, "upload_short", lambda *a: ("vid9", "private"))
    monkeypatch.setattr(tool, "set_thumbnail", lambda *a: True)
    monkeypatch.setattr("sys.argv", ["upload_test.py", str(tmp_path)])
    tool.main()
    out = capsys.readouterr().out
    assert "uploaded: vid9 " in out and out.rstrip().endswith("https://www.youtube.com/shorts/vid9")
    assert yt.videos().list.call_args.kwargs["id"] == "vid9"


def _keychain(monkeypatch, upload, **initial):
    keychain = dict(initial)
    monkeypatch.setattr(upload.keyring, "get_password", lambda service, key: keychain.get(key))
    monkeypatch.setattr(upload.keyring, "set_password", lambda service, key, value: keychain.update({key: value}))
    monkeypatch.setattr(upload.keyring, "delete_password", lambda service, key: keychain.pop(key))
    return keychain


def _flow(monkeypatch, upload, yt):
    """Fake the sign-in; the returned list collects the scopes each flow was built with."""
    seen = []
    flow = MagicMock(); flow.run_local_server.return_value.refresh_token = "tok"
    monkeypatch.setattr(upload.InstalledAppFlow, "from_client_config",
                        lambda config, scopes: seen.append(list(scopes)) or flow)
    yt.channels().list.return_value.execute.return_value = {"items": [{"id": "UC-x", "snippet": {"title": "X"}}]}
    monkeypatch.setattr(upload, "build", lambda *a, **k: yt)
    return seen


@pytest.mark.parametrize("profile", ["primary", "backup"])
def test_auth_asks_for_analytics_only_when_told_and_records_it(tmp_path, monkeypatch, profile):
    from autoclipper import upload
    config = {"installed": {"client_id": "c", "client_secret": "s", "token_uri": "https://oauth2.example/token"}}
    client_json = tmp_path / "client.json"; client_json.write_text(json.dumps(config))
    keychain = _keychain(monkeypatch, upload)
    scopes = _flow(monkeypatch, upload, MagicMock())
    upload.auth(client_json, profile=profile, analytics=True)
    assert scopes == [upload.SCOPES + [upload.ANALYTICS_SCOPE]]
    assert keychain[f"analytics:{profile}"] == "1" and upload.has_analytics(profile)
    upload.auth(client_json, profile=profile)                      # a plain re-auth issues a token without it
    assert scopes[1] == upload.SCOPES and f"analytics:{profile}" not in keychain and not upload.has_analytics(profile)
    upload.auth(client_json, profile=profile)                      # and nothing to remove is fine
    assert scopes[2] == upload.SCOPES


def test_client_asks_for_exactly_the_upload_scopes(monkeypatch):
    from autoclipper import upload
    seen = []
    yt = MagicMock(); yt.channels().list.return_value.execute.return_value = {"items": [{"id": "UC-x"}]}
    _signed_in(monkeypatch, yt)
    monkeypatch.setattr(upload, "Credentials", lambda **kwargs: seen.append(kwargs["scopes"]) or MagicMock())
    client()
    assert seen == [upload.SCOPES] and upload.SCOPES == ["https://www.googleapis.com/auth/youtube.upload",
                                                         "https://www.googleapis.com/auth/youtube.readonly"]


@pytest.mark.parametrize("profile", ["primary", "backup"])
def test_watched_reads_average_percentage_viewed(monkeypatch, profile):
    from autoclipper import upload
    config = {"installed": {"client_id": "c", "client_secret": "s", "token_uri": "https://oauth2.example/token"}}
    _keychain(monkeypatch, upload, client_config=json.dumps(config), refresh_token="r",
              **{"youtube:backup": json.dumps({"client_config": config, "refresh_token": "r", "channel_id": "UC"})})
    monkeypatch.setattr(upload.Credentials, "refresh", lambda self, request: None)
    built = []
    yta = MagicMock()
    yta.reports().query.return_value.execute.return_value = {"rows": [["v1", 900, 61.5]]}   # video, views, percent
    yta.reports().query.reset_mock()
    monkeypatch.setattr(upload, "build", lambda *a, **k: built.append((a, k)) or yta)
    assert upload.watched(profile, ["v1", "v2"], "2026-11-10", "2026-11-18") == {"v1": 61.5}
    assert built[0][0] == ("youtubeAnalytics", "v2")
    assert built[0][1]["credentials"].scopes == upload.SCOPES + [upload.ANALYTICS_SCOPE]
    asked = yta.reports().query.call_args.kwargs
    assert asked["ids"] == "channel==MINE" and asked["filters"] == "video==v1,v2" and asked["dimensions"] == "video"
    assert asked["metrics"] == "views,averageViewPercentage" and asked["sort"] == "-views"   # sorted by a returned metric
    assert asked["maxResults"] <= 200
    assert (asked["startDate"], asked["endDate"]) == ("2026-11-10", "2026-11-18")
    assert upload.watched(profile, [], "2026-11-10", "2026-11-18") == {} and len(built) == 1


def test_watched_asks_in_batches_of_the_row_limit(monkeypatch):
    from autoclipper import upload
    config = {"installed": {"client_id": "c", "client_secret": "s", "token_uri": "https://oauth2.example/token"}}
    _keychain(monkeypatch, upload, client_config=json.dumps(config), refresh_token="r")
    monkeypatch.setattr(upload.Credentials, "refresh", lambda self, request: None)
    yta = MagicMock(); yta.reports().query.return_value.execute.return_value = {}     # no rows: nothing to report
    yta.reports().query.reset_mock()
    monkeypatch.setattr(upload, "build", lambda *a, **k: yta)
    assert upload.watched("primary", [f"v{i}" for i in range(250)], "2026-11-10", "2026-11-18") == {}
    assert [len(c.kwargs["filters"].split(",")) for c in yta.reports().query.call_args_list] == [200, 50]


@pytest.mark.parametrize("status,reason,kind", [
    (401, "quotaExceeded", "credential"), (429, "quotaExceeded", "transient"), (503, "dailyLimitExceeded", "transient"),
    (403, "quotaExceeded", "quota"),
])
def test_client_lets_the_http_status_decide_before_a_quota_reason(monkeypatch, status, reason, kind):
    from autoclipper.upload import http_failure
    yt = MagicMock(); yt.channels().list.return_value.execute.side_effect = http_error(reason, status=status)
    _signed_in(monkeypatch, yt)
    if kind == "quota":
        with pytest.raises(QuotaExhausted):
            client()
        return
    with pytest.raises(HttpError) as raised:                 # not translated to quota: the caller classifies it
        client()
    assert http_failure(raised.value)[0] == kind
