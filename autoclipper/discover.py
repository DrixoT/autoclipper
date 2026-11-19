"""Discover live streams.

Kick's public ``/api/search?searched_word={game}`` returns categories with ``id``,
``name`` (e.g. "Grand Theft Auto VI (GTA)") and ``slug``.
``/stream/livestreams/en?page={page}&limit=24&subcategory={slug}&sort=desc`` returns that
category's live streams, biggest first, as ``data`` records with ``language`` (a name such as
"English"; the ``/en`` path does not filter it), ``viewer_count``, ``channel.slug``,
``channel.user.username`` and ``categories``. Checked 2026-11-17: ``subcategories={id}`` is
ignored, and without ``sort=desc`` the smallest streams come first.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import keyring


TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API = "https://api.twitch.tv/helix"
KICK_SEARCH_URL = "https://kick.com/api/search"
KICK_STREAMS_URL = "https://kick.com/stream/livestreams/en"
KICK_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
KICK_PAGE_SIZE = 24
GAME_RETRY_S = 600
CLIP_WINDOW_S = 7 * 86400          # a streamer allows clipping when someone clipped them within this window
KICK_MAX_PAGES = 3                 # sorted biggest first, so later pages are far below min_viewers


@dataclass(frozen=True)
class Live:
    platform: str
    channel: str
    name: str
    viewers: int
    game: str
    id: str = ""        # Twitch broadcaster id, for the clips check


def twitch_credentials() -> tuple[str, str] | None:
    client_id = keyring.get_password("autoclipper", "twitch_client_id")
    secret = keyring.get_password("autoclipper", "twitch_client_secret")
    return (client_id, secret) if client_id and secret else None


def _http(method: str, url: str, headers: dict, body: bytes | None = None,
          timeout: float = 20) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        try:
            payload = json.loads(response.read())
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        return response.status, payload if isinstance(payload, dict) else {}


class Helix:
    def __init__(self, client_id: str, secret: str, *, http=_http):
        self.client_id = client_id
        self.secret = secret
        self.http = http
        self._token: str | None = None
        self._games: dict[str, tuple[str | None, float]] = {}    # name -> (id or None, when asked)

    def token(self) -> str:
        if self._token is None:
            body = urllib.parse.urlencode({"client_id": self.client_id,
                                           "client_secret": self.secret,
                                           "grant_type": "client_credentials"}).encode()
            status, payload = self.http("POST", TWITCH_TOKEN_URL,
                                        {"Content-Type": "application/x-www-form-urlencoded"}, body)
            if status != 200:
                raise RuntimeError(f"Twitch token request failed with status {status}")
            self._token = payload["access_token"]
        return self._token

    def game_id(self, name: str) -> str | None:
        """Found ids are kept; a game Twitch does not know yet is asked again after GAME_RETRY_S."""
        found, when = self._games.get(name, (None, None))
        if found is None and (when is None or time.time() - when >= GAME_RETRY_S):
            status, payload = self.http(
                "GET", f"{TWITCH_API}/games?{urllib.parse.urlencode({'name': name})}",
                {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token()}"})
            if status != 200:
                raise RuntimeError(f"Twitch game request failed with status {status}")
            rows = payload.get("data", [])
            self._games[name] = (rows[0]["id"] if rows else None, time.time())
        return self._games[name][0]

    def streams(self, game_id: str, language: str, first: int = 100) -> list[Live]:
        query = urllib.parse.urlencode({"game_id": game_id, "type": "live",
                                        "language": language, "first": first})
        url = f"{TWITCH_API}/streams?{query}"
        for attempt in range(2):
            status, payload = self.http("GET", url, {
                "Client-Id": self.client_id, "Authorization": f"Bearer {self.token()}"})
            if status == 401 and attempt == 0:
                self._token = None
                continue
            if status != 200:
                raise RuntimeError(f"Twitch streams request failed with status {status}")
            return [Live("twitch", row["user_login"].lower(), row["user_name"],
                         int(row["viewer_count"]), row["game_name"], str(row.get("user_id", "")))
                    for row in payload.get("data", [])]
        raise RuntimeError("Twitch streams request failed with status 401")


    def category(self, login: str) -> str | None:
        """The category (game) one channel is streaming now, or None when it is offline."""
        url = f"{TWITCH_API}/streams?{urllib.parse.urlencode({'user_login': login})}"
        for attempt in range(2):
            status, payload = self.http("GET", url, {
                "Client-Id": self.client_id, "Authorization": f"Bearer {self.token()}"})
            if status == 401 and attempt == 0:
                self._token = None
                continue
            if status != 200:
                raise RuntimeError(f"Twitch stream request failed with status {status}")
            rows = payload.get("data") or []
            return (rows[0].get("game_name") or None) if rows else None
        raise RuntimeError("Twitch stream request failed with status 401")

    def recent_clip(self, broadcaster_id: str) -> bool:
        """True when the channel has a clip from the last 7 days: clipping is on and open to viewers."""
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - CLIP_WINDOW_S))
        query = urllib.parse.urlencode({"broadcaster_id": broadcaster_id, "started_at": since, "first": 1})
        status, payload = self.http("GET", f"{TWITCH_API}/clips?{query}",
                                    {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token()}"})
        if status != 200:
            raise RuntimeError(f"Twitch clips request failed with status {status}")
        return bool(payload.get("data"))


def kick_recent_clip(slug: str, *, http=_http) -> bool:
    """True when the Kick channel has a clip from the last week (the owner checked this endpoint: no login)."""
    status, payload = http("GET", f"https://kick.com/api/v2/channels/{urllib.parse.quote(slug)}/clips?sort=date&time=week",
                           {"User-Agent": KICK_UA, "Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"Kick clips request failed with status {status}")
    return bool(payload.get("clips"))


def kick_category(slug: str, *, http=_http) -> str | None:
    """The category one Kick channel is streaming now, or None when it is offline."""
    status, payload = http("GET", f"https://kick.com/api/v2/channels/{urllib.parse.quote(slug)}",
                           {"User-Agent": KICK_UA, "Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"Kick channel request failed with status {status}")
    live = payload.get("livestream")
    categories = live.get("categories") if isinstance(live, dict) else None
    name = categories[0].get("name") if categories and isinstance(categories[0], dict) else None
    return name if isinstance(name, str) and name else None


def _kick_name(name: str) -> str:
    # Kick sometimes appends a short franchise label, e.g. "(GTA)".
    return name.partition(" (")[0].strip().casefold()


def _language_matches(stream_language: str, language: str) -> bool:
    language_names = {"en": "english", "es": "spanish", "fr": "french",
                      "de": "german", "pt": "portuguese", "it": "italian",
                      "ja": "japanese", "ko": "korean", "ru": "russian"}
    wanted = language.casefold()
    return stream_language.casefold() in (wanted, language_names.get(wanted, wanted))


def kick_streams(game: str, language: str, *, http=_http) -> list[Live]:
    headers = {"User-Agent": KICK_UA, "Accept": "application/json"}
    search_url = f"{KICK_SEARCH_URL}?{urllib.parse.urlencode({'searched_word': game})}"
    status, search = http("GET", search_url, headers)
    if status != 200:
        raise RuntimeError(f"Kick category search failed with status {status}")
    target = _kick_name(game)
    categories = search.get("categories", [])
    category = next((item for item in categories
                     if isinstance(item.get("name"), str) and _kick_name(item["name"]) == target), None)
    if category is None:
        return []

    category_id, category_name, slug = category["id"], category["name"], category["slug"]
    result = []
    for page in range(1, KICK_MAX_PAGES + 1):
        query = urllib.parse.urlencode({"page": page, "limit": KICK_PAGE_SIZE, "subcategory": slug,
                                        "sort": "desc"})
        status, payload = http("GET", f"{KICK_STREAMS_URL}?{query}", headers)
        if status != 200:
            raise RuntimeError(f"Kick livestream request failed with status {status}")
        rows = payload.get("data", [])
        for row in rows:
            try:
                categories = row["categories"]
                if not any(int(item["id"]) == int(category_id) for item in categories):
                    continue
                if not _language_matches(row["language"], language):
                    continue
                channel = row["channel"]
                result.append(Live("kick", channel["slug"].lower(), channel["user"]["username"],
                                   int(row.get("viewer_count", row.get("viewers", 0))), category_name))
            except (KeyError, TypeError, ValueError):
                continue
        if not rows or not payload.get("next_page_url"):
            break
    return result


class ProbeFailed(RuntimeError):
    """streamlink gave no usable answer: it timed out, could not run, exited non-zero or printed bad JSON."""


def live_category(url: str, *, run=subprocess.run) -> str | None:
    """The live category streamlink reports for a stream, None when it answered without one; ProbeFailed when it
    gave no usable answer, so a caller can keep what it knew instead of clearing it."""
    argv = [str(Path(sys.executable).with_name("streamlink")), "--json", url]
    try:
        result = run(argv, timeout=60, capture_output=True, text=True)
        payload = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise ProbeFailed(f"streamlink: {type(e).__name__}: {e}") from e
    if result.returncode != 0 or not isinstance(payload, dict) or "error" in payload:
        error = payload.get("error") if isinstance(payload, dict) else None
        raise ProbeFailed(f"streamlink exited {result.returncode}: {error or 'no usable answer'}")
    metadata = payload.get("metadata")
    category = metadata.get("category") if isinstance(metadata, dict) else None
    return category if isinstance(category, str) and category else None


def probe(url: str, *, run=subprocess.run) -> tuple[bool, str | None]:
    argv = [str(Path(sys.executable).with_name("streamlink")), "--json", url]
    try:
        result = run(argv, timeout=60, capture_output=True, text=True)
        if result.returncode != 0:
            return False, None
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or "error" in payload:
            return False, None
        streams = payload.get("streams")
        metadata = payload.get("metadata")
        category = metadata.get("category") if isinstance(metadata, dict) else None
        return bool(streams), category if isinstance(category, str) and category else None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False, None
