import base64
import json
import logging
import math
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import keyring
from PIL import Image

from autoclipper.meta import timed_lines


API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
PRICE_PER_TOKEN = 0.042 / 1_000_000
TRIES = 4
MAX_WAIT_S = 30                # a Retry-After longer than this is not honoured
DEADLINE_S = 90                # past this, Jev is unavailable for this candidate and the slot falls back
QUESTIONS = {
    "hook": {
        "type": "score",
        "instructions": "If this moment were posted as a short video starting a few seconds before `chat_spike_starts_at_second`, how strongly would its opening seconds grab someone scrolling short videos who knows nothing about `streamer`? Use `transcript`, `chat_per_second` and `frames`.",
        "criteria": [
            "Nothing happens or it is confusing; a viewer would scroll away",
            "Mild interest; nothing stands out yet",
            "Something intriguing is set up early",
            "A strong, clear reason to keep watching appears within seconds",
            "Immediately gripping: a shocking, funny or dramatic moment right at the start",
        ],
    },
    "context": {
        "type": "score",
        "instructions": "Could someone who has never watched `streamer` or played `game` understand what happens in this clip from the clip alone?",
        "criteria": ["No: it depends on inside jokes or earlier events", "Mostly not", "Partly", "Mostly yes", "Yes: fully self-explanatory"],
    },
    "payoff": {
        "type": "score",
        "instructions": "Does the clip build to a clear moment that pays off (a reveal, a fail, a win, a punchline, a reaction)?",
        "criteria": ["No payoff", "A weak or unclear payoff", "A clear payoff", "A strong, memorable payoff"],
    },
    "crowd": {
        "type": "score",
        "instructions": "How strongly does the chat react around `chat_spike_starts_at_second`, judging by `chat_per_second`?",
        "criteria": ["Barely any reaction", "Some reaction", "A clear spike of reactions", "An explosion of reactions"],
    },
    "appeal": {
        "type": "score",
        "instructions": "How appealing would this clip be to people who are not fans of `streamer`?",
        "criteria": ["Only fans would care", "Of some interest to others", "Broadly entertaining", "Likely to entertain almost anyone"],
    },
    "story": {
        "type": "score",
        "instructions": "Does the window around `chat_spike_starts_at_second` show a setup a newcomer can follow, the actual on-stream event, and the streamer's reaction, in that order?",
        "criteria": ["No clear event", "An event without its setup or reaction",
                     "Setup and event, weak reaction", "Setup, event and reaction all clear"],
    },
    "dead": {
        "type": "noul",
        "instructions": "Is most of this clip dead air, a menu, a loading screen, a holding or 'be right back' screen, or an advertisement?",
    },
    "nude": {
        "type": "noul",
        "instructions": "Does any frame show nudity, a character undressed or in underwear, showering or bathing, "
                        "sexual content, or a strip club, even inside a video game?",
    },
}
NUDE_MAX = 0.3                       # at or above this the clip is never posted: YouTube flags nudity, game or not
WEIGHTS = {"hook": 0.20, "context": 0.10, "payoff": 0.20, "crowd": 0.10, "appeal": 0.20, "story": 0.20}
MIN_RANK = 0.45
_BACKOFF = (2, 4, 8)
_log = logging.getLogger("autoclipper")


class JevError(Exception):
    """A Jev request or response could not be used."""


_KEY: list[str] = []          # the key once found: the daemon asks every 15 s, the Keychain only once


def api_key() -> str | None:
    if not _KEY:
        key = keyring.get_password("autoclipper", "typesafe_api_key")
        if not key:
            return None
        _KEY.append(key)
    return _KEY[0]


def _chat_line(entry: dict) -> str:
    top = entry.get("top", [])
    if not isinstance(top, list):
        top = []
    reactions = ", ".join(str(message) for message in top[:3])
    line = f"{entry.get('s')}: {entry.get('n')} msgs"
    return f"{line} | {reactions}" if reactions else line


def _second_label(second: float) -> str:
    return str(int(second)) if float(second).is_integer() else f"{second:g}"


def build_request(streamer: str, game: str, mood: str, onset_s: float, words,
                  chat: list[dict], frames: list[tuple[float, Path]]) -> dict:
    state = {
        "streamer": streamer,
        "game": game,
        "chat_mood": mood,
        "chat_spike_starts_at_second": round(onset_s),
        "transcript": timed_lines(words),
        "chat_per_second": [_chat_line(entry) for entry in chat if entry.get("n", 0)],
        "frames": {},
    }
    assets = {}
    for second, path in frames:
        try:
            with Image.open(path) as image:
                width, height = image.size
            if not (280 <= width <= 1568 and 280 <= height <= 1568):
                continue
            data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except (OSError, ValueError):
            continue
        ref = uuid.uuid4().hex
        state["frames"][f"at_{_second_label(second)}s"] = ref
        assets[ref] = {"type": "image", "format": "jpg", "data_base64": data}
    body = {"model": MODEL, "state": state, "questions": QUESTIONS}
    if assets:
        body["assets"] = assets
    return body


def _post(url: str, body: bytes, headers: dict, timeout: float = 60) -> tuple[int, dict, dict]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            response_headers = dict(response.headers.items())
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        response_headers = dict(error.headers.items()) if error.headers else {}
        raw = error.read()
    try:
        payload = json.loads(raw) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    return status, payload if isinstance(payload, dict) else {}, response_headers


def _retry_after(headers: dict) -> float | None:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def ask(body: dict, key: str, *, post=_post, sleep=time.sleep, clock=time.monotonic) -> dict:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if body.get("assets"):
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    started = clock()

    def pause(wait: float) -> None:
        wait = min(wait, MAX_WAIT_S)
        if clock() - started + wait > DEADLINE_S:
            raise JevError(f"Jev did not answer within {DEADLINE_S} s")
        sleep(wait)

    for attempt in range(TRIES):
        try:
            timeout = max(1.0, min(60.0, DEADLINE_S - (clock() - started)))
            status, response, response_headers = post(API, encoded, headers, timeout)
        except OSError as error:
            if attempt + 1 == TRIES:
                raise JevError("Jev request failed after retries") from error
            pause(_BACKOFF[attempt])
            continue
        if 200 <= status < 300:
            try:
                tokens = response["usage"]["input_tokens"]
                if isinstance(tokens, bool) or not isinstance(tokens, (int, float)) or not math.isfinite(tokens) or tokens < 0:
                    raise ValueError("invalid input_tokens")
            except (KeyError, TypeError, ValueError, OverflowError):
                tokens = 0
            _log.info("Jev input_tokens=%s cost=$%.8f", tokens, tokens * PRICE_PER_TOKEN)
            return response
        retryable = status in (429, 529) or 500 <= status <= 599
        if not retryable:
            raise JevError(f"Jev request failed with HTTP {status}")
        if attempt + 1 == TRIES:
            raise JevError(f"Jev request failed with HTTP {status} after retries")
        wait = _retry_after(response_headers)
        pause(_BACKOFF[attempt] if wait is None else wait)
    raise JevError("Jev request failed after retries")


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def scores(response: dict) -> dict[str, float]:
    try:
        answers = response["answers"]
        result = {}
        for name, question in QUESTIONS.items():
            answer = answers[name]
            if not isinstance(answer, dict) or answer.get("type") != question["type"]:
                raise ValueError
            if question["type"] == "noul":
                value = answer["noul"]
                if not _number(value) or not 0 <= value <= 1:
                    raise ValueError
                result[name] = float(value)
            else:
                value, levels = answer["score"], len(question["criteria"])
                if not _number(value) or not 0 <= value <= levels - 1:
                    raise ValueError
                result[name] = float(value) / (levels - 1)
        return result
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise JevError("Jev response is missing a valid answer") from error


def rank(s: dict[str, float], crowd: bool = True, weights: dict[str, float] | None = None) -> float:
    """Weighted mean of the questions, scaled down by dead air. Without chat (a clip cut from a recording)
    the crowd question has nothing to judge, so it is left out and the other weights carry the mean.
    `weights` replaces the hand-set WEIGHTS with learned ones."""
    if s.get("nude", 0.0) >= NUDE_MAX:           # .get: ranks stored before this question have no answer
        return 0.0
    w = WEIGHTS if weights is None else weights
    if not crowd:
        w = {k: v for k, v in w.items() if k != "crowd"}
    return sum(w[key] * s[key] for key in w) / sum(w.values()) * (1 - s["dead"])
