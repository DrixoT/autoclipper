"""Publish local vertical videos through Meta's Instagram API with Facebook Login."""

import json
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import keyring

from .store import next_output, paused, put_output, set_output

GRAPH_VERSION = "v27.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
UPLOAD = f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}"
FORMAT = "instagram_reel"
CHECK_EVERY_S = 60
PROCESS_LIMIT_S = 5 * 60
MAX_PREPARATION_ERRORS = 3
ACCOUNT_ID = re.compile(r"[0-9]+")
KEEP_PENDING_S = 72 * 3600


class InstagramError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _request(method, url, token, *, data=None, headers=None, auth="Bearer", timeout=30):
    req = Request(url, data=data, headers=headers or {}, method=method)
    req.add_unredirected_header("Authorization", f"{auth} {token}")
    try:
        with urlopen(req, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise InstagramError(f"Instagram returned HTTP {error.code}",
                             retryable=error.code == 429 or error.code >= 500) from None
    except (URLError, OSError) as error:
        raise InstagramError("Instagram network request failed", retryable=True) from error
    except ValueError as error:
        raise InstagramError("Instagram returned an invalid response") from error
    if not isinstance(payload, dict) or payload.get("error"):
        raise InstagramError("Instagram rejected the request")
    return payload


def _id(payload: dict) -> str:
    value = str(payload.get("id", ""))
    if not ACCOUNT_ID.fullmatch(value):
        raise InstagramError("Instagram did not return a valid ID")
    return value


class Instagram:
    def __init__(self, account_id: str, token: str, *, request=_request):
        if not ACCOUNT_ID.fullmatch(account_id):
            raise ValueError("Instagram account ID must contain digits only")
        self.account_id = account_id
        self.token = token
        self.request = request

    def _graph(self, method: str, endpoint: str, *, query=None, fields=None) -> dict:
        url = f"{GRAPH}/{endpoint}"
        if query:
            url += "?" + urlencode(query)
        data = json.dumps(fields).encode() if fields is not None else None
        return self.request(method, url, self.token, data=data, headers={"Content-Type": "application/json"})

    def username(self) -> str:
        account = self._graph("GET", self.account_id, query={"fields": "id,username"})
        if _id(account) != self.account_id or not isinstance(account.get("username"), str):
            raise InstagramError("Meta authorization could not verify the configured Instagram account")
        return account["username"]

    def stage_reel(self, video: Path, caption: str, cover_ms: int) -> str:
        size = video.stat().st_size
        if size <= 0:
            raise InstagramError("Reel is empty")
        container = _id(self._graph("POST", f"{self.account_id}/media", fields={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption, "thumb_offset": cover_ms,
        }))
        with video.open("rb") as stream:
            result = self.request("POST", f"{UPLOAD}/{container}", self.token, data=stream, auth="OAuth",
                                  headers={"offset": "0", "file_size": str(size), "Content-Length": str(size),
                                           "Content-Type": "application/octet-stream"}, timeout=120)
        if result.get("success") is not True:
            raise InstagramError("Meta did not confirm receipt of the Reel")
        return container

    def status(self, container: str) -> str:
        return self._graph("GET", _id({"id": container}), query={"fields": "status_code"}).get("status_code", "")

    def publish(self, container: str) -> str:
        return _id(self._graph("POST", f"{self.account_id}/media_publish", fields={
            "creation_id": _id({"id": container}),
        }))


def authorize(account_id: str, token: str) -> str:
    instagram = Instagram(account_id, token.strip())
    username = instagram.username()
    keyring.set_password("autoclipper", f"instagram_page_token:{account_id}", instagram.token)
    return username


def queue(db, clip_id: str, profile: str, account_id: str, video: Path, thumb: Path,
          title: str, description: str, credit: str, cover_ms: int) -> None:
    caption = f"{title}\n\n{description}\n\n{credit}"
    put_output(db, clip_id, FORMAT, "pending", {
        "account_id": account_id, "caption": caption, "cover_ms": cover_ms,
    }, str(video), str(thumb), profile)


def advance(db, profile: str, account_id: str, stop, *, now=time.time, logger=None) -> str | None:
    """Make one bounded step for the Reels of one YouTube profile, held while that profile is paused. A publish
    attempt is never repeated after an uncertain result."""
    if stop.is_set() or paused(db, profile):
        return None
    row = next_output(db, profile, now() - KEEP_PENDING_S, fmt=FORMAT, account_id=account_id, public_only=True)
    if row is None:
        return None
    token = keyring.get_password("autoclipper", f"instagram_page_token:{account_id}")
    if not token:
        if logger:
            logger.warning("Instagram Reel waits for authorization for account %s", account_id)
        return None

    clip_id = row["clip_id"]
    info = json.loads(row["meta"])
    instagram = Instagram(account_id, token)
    container = info.get("container")
    try:
        if container is None:
            if stop.is_set():
                return None
            info["container"] = instagram.stage_reel(Path(row["video_path"]), info["caption"], info["cover_ms"])
            info["staged_at"] = now()
            set_output(db, clip_id, FORMAT, "pending", meta=json.dumps(info), error=None)
            return None

        status = instagram.status(container)
        if status == "IN_PROGRESS" and now() - info["staged_at"] <= PROCESS_LIMIT_S:
            return None
        if status != "FINISHED":
            raise InstagramError(f"Reel container did not become ready ({status or 'unknown'})")
    except (InstagramError, OSError) as error:
        failures = info.get("failures", 0) + 1
        info["failures"] = failures
        retry = isinstance(error, InstagramError) and error.retryable and failures < MAX_PREPARATION_ERRORS
        message = str(error) if isinstance(error, InstagramError) else type(error).__name__
        set_output(db, clip_id, FORMAT, "pending" if retry else "failed", error=message, meta=json.dumps(info))
        if logger:
            logger.warning("%s: Instagram preparation %s (%s)", clip_id, "will retry" if retry else "failed", message)
        return None

    if stop.is_set() or paused(db, profile):
        return None
    set_output(db, clip_id, FORMAT, "uploading")
    try:
        media_id = instagram.publish(container)
    except InstagramError as error:
        set_output(db, clip_id, FORMAT, "failed", error=f"Reel publish not confirmed: {error}")
        if logger:
            logger.warning("%s: Reel publish not confirmed for container %s", clip_id, container)
        return None
    set_output(db, clip_id, FORMAT, "uploaded", video_id=media_id, uploaded_at=now(), error=None)
    if logger:
        logger.info("%s: Instagram Reel published as %s", clip_id, media_id)
    return media_id
