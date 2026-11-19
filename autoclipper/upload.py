import json
import time
from pathlib import Path
from typing import Sequence

import keyring
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .config import DEFAULT_PROFILE, validate_profile
from .meta import Meta


SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"   # asked for only by auth(analytics=True)
ANALYTICS_ROWS = 200                # reports.query allows at most 200 rows (maxResults) when dimensions=video
QUOTA_REASONS = ("quotaExceeded", "dailyLimitExceeded")
CHANNELS: dict[str, str] = {}      # profile -> the channel ID client() last verified, for the Studio fallback


class UploadLimit(Exception):
    pass


class QuotaExhausted(Exception):
    pass


class SignInError(RuntimeError):
    """The stored sign-in itself is unusable (missing, incomplete, or on another channel): someone must sign in."""


def _named_profile(profile: str) -> dict:
    raw = keyring.get_password("autoclipper", f"youtube:{validate_profile(profile)}")
    return json.loads(raw) if raw else {}


def auth(client_secret_json: Path | None = None, *, profile: str = DEFAULT_PROFILE, analytics: bool = False) -> dict:
    """Authorize a channel and save its credentials to the macOS Keychain. With analytics, also ask for the
    permission to read its Analytics and remember that it was given."""
    validate_profile(profile)
    saved = _named_profile(profile) if profile != DEFAULT_PROFILE else {}
    if client_secret_json:
        client_config = json.loads(client_secret_json.read_text())
    elif profile == DEFAULT_PROFILE:
        client_config = json.loads(keyring.get_password("autoclipper", "client_config") or "null")
    else:
        client_config = saved.get("client_config") or json.loads(
            keyring.get_password("autoclipper", "client_config") or "null"
        )
    if not client_config:
        raise RuntimeError("first sign-in needs the client JSON: `autoclipper auth <client json>`")

    scopes = SCOPES + [ANALYTICS_SCOPE] if analytics else SCOPES
    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="select_account consent",
                                        timeout_seconds=600, authorization_prompt_message=None)
    if not credentials.refresh_token:
        raise RuntimeError("Google did not supply a refresh token. Complete the consent screen and try again.")
    yt = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    channel = _channel(yt)
    if saved and channel["id"] != saved["channel_id"]:
        raise RuntimeError(f"Profile {profile!r} is already linked to another YouTube channel")
    if profile == DEFAULT_PROFILE:
        keyring.set_password("autoclipper", "client_config", json.dumps(client_config))
        keyring.set_password("autoclipper", "refresh_token", credentials.refresh_token)
    else:
        keyring.set_password("autoclipper", f"youtube:{profile}", json.dumps({
            "client_config": client_config,
            "refresh_token": credentials.refresh_token,
            "channel_id": channel["id"],
        }))
    if analytics:
        keyring.set_password("autoclipper", f"analytics:{profile}", "1")
    elif has_analytics(profile):       # a token issued without the permission replaces the one that had it
        keyring.delete_password("autoclipper", f"analytics:{profile}")
    return channel


def has_analytics(profile: str) -> bool:
    """Whether this profile was signed in with the Analytics permission."""
    return keyring.get_password("autoclipper", f"analytics:{validate_profile(profile)}") == "1"


def _channel(yt) -> dict:
    try:
        items = yt.channels().list(part="id,snippet", mine=True).execute().get("items") or []
    except HttpError as error:
        if _reason(error) != "youtubeSignupRequired":
            raise
        items = []
    if len(items) != 1 or not items[0].get("id"):
        raise SignInError("the signed-in Google account has no YouTube channel selected; run `autoclipper auth` "
                           "and pick the channel's account")
    return items[0]


def client(profile: str = DEFAULT_PROFILE) -> tuple:
    """An API client for a profile and the channel ID verified while building it: (yt, channel_id)."""
    if profile == DEFAULT_PROFILE:
        raw = keyring.get_password("autoclipper", "client_config")
        refresh_token = keyring.get_password("autoclipper", "refresh_token")
        client_config = json.loads(raw) if raw else None
        channel_id = None
    else:
        saved = _named_profile(profile)
        client_config = saved.get("client_config")
        refresh_token = saved.get("refresh_token")
        channel_id = saved.get("channel_id")
        if client_config and refresh_token and not channel_id:
            raise SignInError(f"YouTube profile {profile!r} is missing its channel ID")
    if not client_config or not refresh_token:
        raise SignInError(f"run `autoclipper auth --profile {profile} <client json>` first")

    details = client_config.get("installed") or client_config.get("web") or {}
    credentials = Credentials(
        token=None, refresh_token=refresh_token, token_uri=details.get("token_uri"),
        client_id=details.get("client_id"), client_secret=details.get("client_secret"),
        scopes=SCOPES,
    )
    credentials.refresh(Request())
    yt = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    try:
        channel = _channel(yt)
    except HttpError as error:
        if http_failure(error)[0] == "quota":     # the API quota is gone, not the authorisation
            raise QuotaExhausted() from error
        raise                                     # the status decides first: a 401 or a 429/5xx is never quota
    if channel_id and channel["id"] != channel_id:
        raise SignInError(f"YouTube authorization for profile {profile!r} resolved to a different channel")
    CHANNELS[profile] = channel["id"]
    return yt, channel["id"]


# OAuth errors that mean the stored sign-in itself is no longer valid; any other refresh failure is transient
DEFINITIVE_SIGN_IN = ("invalid_grant", "invalid_client", "unauthorized_client", "deleted_client", "revoked")


def definitive(error: Exception) -> bool:
    """True when a token refresh failed for good (expired or revoked grant, deleted or unauthorised client), so a
    person must sign in again; False for a network blip or a server error at Google, which a retry can fix."""
    text = " ".join(map(str, error.args)).lower()
    return not getattr(error, "retryable", False) and any(code in text for code in DEFINITIVE_SIGN_IN)


# 403 reasons that say the sign-in itself lacks rights; a 401 always means the credentials were refused
CREDENTIAL_REASONS = ("insufficientPermissions", "insufficientScope", "authError", "invalidCredentials")


def http_failure(error: HttpError) -> tuple[str, str]:
    """(kind, reason) for an API error while signing in, the HTTP status first: "credential" for any 401 and for a
    403 with an explicit credential or scope reason; "transient" for any 429 or 5xx whatever its reason, and for a
    plain forbidden, a rate or limit reason or an unknown reason, which a later pass can retry; "quota" only for a
    quota reason on any other status."""
    reason, status = _reason(error) or "", error.resp.status
    if status == 401 or (status == 403 and reason in CREDENTIAL_REASONS):
        return "credential", reason
    if status == 429 or status >= 500 or reason not in QUOTA_REASONS:
        return "transient", reason
    return "quota", reason


def verified_channel(profile: str) -> str | None:
    """The channel ID client() verified for this profile in this process, without another API call."""
    return CHANNELS.get(profile)


def credit_line(streamer: str, url: str) -> str:
    return f"Clipped from {streamer}'s live stream: {url}"


def _reason(error: HttpError) -> str | None:
    try:
        content = error.content
        if isinstance(content, bytes):
            content = content.decode()
        return json.loads(content)["error"]["errors"][0]["reason"]
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def describe(meta: Meta, credit: str, short: bool = True) -> str:
    """The posted description: the editor's text, the credit line, and #shorts only on a Short."""
    return f"{meta.description}\n\n{credit}" + ("\n#shorts" if short else "")


def upload_short(yt, video: Path, meta: Meta, credit: str, privacy: str, *,
                 short: bool = True) -> tuple[str, str | None]:
    """Upload a Short, or with short=False a regular video (no #shorts marker). Returns the video ID and the
    privacy YouTube reports it gave the video (None when the response does not say)."""
    body = {
        "snippet": {
            "title": meta.title,
            "description": describe(meta, credit, short),
            "tags": list(meta.tags),
            "categoryId": "20",                            # Gaming
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    request = yt.videos().insert(part="snippet,status", body=body,
                                 media_body=MediaFileUpload(str(video), resumable=True))
    response, stalls, progress = None, 0, request.resumable_progress
    try:
        while response is None:
            _, response = request.next_chunk(num_retries=3)
            stalls = stalls + 1 if response is None and request.resumable_progress == progress else 0
            progress = request.resumable_progress
            if stalls >= 5:
                raise RuntimeError("upload stalled")
    except HttpError as error:
        reason = _reason(error)
        if reason == "uploadLimitExceeded":
            raise UploadLimit() from error
        if reason in QUOTA_REASONS:
            raise QuotaExhausted() from error
        raise
    return response["id"], (response.get("status") or {}).get("privacyStatus")


def wait_processed(yt, video_id: str, timeout_s: float = 600, every_s: float = 15,
                   sleep=time.sleep, now=time.monotonic) -> bool:
    """True once YouTube has processed the video; a Short's thumbnail can only be set after that.
    Measured 2026-11-17: a 10 s Short took 30 s."""
    deadline = now() + timeout_s
    while True:
        items = yt.videos().list(part="status,processingDetails", id=video_id).execute().get("items") or []
        if items:
            status = (items[0].get("processingDetails") or {}).get("processingStatus")
            upload = (items[0].get("status") or {}).get("uploadStatus")
            if status == "succeeded" or upload == "processed":
                return True
            if status in ("failed", "terminated") or upload in ("failed", "rejected", "deleted"):
                return False
        if now() >= deadline:
            return False
        sleep(every_s)


def set_thumbnail(yt, video_id: str, jpg: Path) -> bool:
    try:
        yt.thumbnails().set(videoId=video_id,
                            media_body=MediaFileUpload(str(jpg), mimetype="image/jpeg")).execute()
        return True
    except HttpError as error:
        if error.resp.status in (400, 403, 404):          # a refusal; anything else is a real failure
            return False
        raise


def video_stats(yt, ids: Sequence[str]) -> dict[str, tuple[int, int]]:
    """(views, likes) per video, 50 IDs a request; videos YouTube does not return (deleted, private) are absent,
    and hidden likes count as 0."""
    stats = {}
    for start in range(0, len(ids), 50):
        result = yt.videos().list(part="statistics", id=",".join(ids[start:start + 50])).execute()
        for item in result.get("items", []):
            counts = item.get("statistics", {})
            stats[item["id"]] = (int(counts.get("viewCount", 0)), int(counts.get("likeCount", 0)))
    return stats


def _report(ids: Sequence[str], start: str, end: str) -> dict:
    """Request parameters of the per-video report (reports.query, 'Top videos'); the only place they are named."""
    # views is requested only because the report must be sorted, and it sorts by a metric it returns
    return dict(ids="channel==MINE", metrics="views,averageViewPercentage", dimensions="video",
                filters="video==" + ",".join(ids), startDate=start, endDate=end, sort="-views",
                maxResults=ANALYTICS_ROWS)


def watched(profile: str, ids: Sequence[str], start: str, end: str) -> dict[str, float]:
    """Average percentage viewed of each video over start..end (YYYY-MM-DD); videos with no data are absent.
    Builds its own credentials: the Analytics scope is never added to the upload client."""
    if not ids:
        return {}
    if profile == DEFAULT_PROFILE:
        raw = keyring.get_password("autoclipper", "client_config")
        client_config = json.loads(raw) if raw else None
        refresh_token = keyring.get_password("autoclipper", "refresh_token")
    else:
        saved = _named_profile(profile)
        client_config, refresh_token = saved.get("client_config"), saved.get("refresh_token")
    if not client_config or not refresh_token:
        raise RuntimeError(f"run `autoclipper auth --profile {profile} --analytics` first")
    details = client_config.get("installed") or client_config.get("web") or {}
    credentials = Credentials(
        token=None, refresh_token=refresh_token, token_uri=details.get("token_uri"),
        client_id=details.get("client_id"), client_secret=details.get("client_secret"),
        scopes=SCOPES + [ANALYTICS_SCOPE],
    )
    credentials.refresh(Request())
    yta = build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)
    found = {}
    for i in range(0, len(ids), ANALYTICS_ROWS):
        answer = yta.reports().query(**_report(ids[i:i + ANALYTICS_ROWS], start, end)).execute()
        for row in answer.get("rows") or []:      # columns: the video dimension, then the metrics in order
            found[row[0]] = float(row[-1])
    return found


def strike_check(yt, video_ids: Sequence[str]) -> list[str]:
    bad = set()
    found = set()
    for start in range(0, len(video_ids), 50):
        batch = list(video_ids[start:start + 50])
        result = yt.videos().list(part="status", id=",".join(batch)).execute()
        for item in result.get("items", []):
            video_id = item.get("id")
            if video_id is not None:
                found.add(video_id)
                if item.get("status", {}).get("uploadStatus") in ("rejected", "deleted"):
                    bad.add(video_id)
    return [video_id for video_id in video_ids if video_id in bad or video_id not in found]
