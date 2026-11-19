import asyncio
import http.client
import json
import math
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

import websockets
from websockets.exceptions import WebSocketException

from autoclipper.config import PLATFORMS, StreamCfg


@dataclass(frozen=True)
class ChatMessage:
    ts: float
    user: str
    text: str


def parse_twitch_line(line: str, now: float) -> ChatMessage | None:
    if line.startswith("@"):                       # IRCv3 tags
        line = line.partition(" ")[2]
    parts = line.split(" ", 3)                     # :nick!user@host PRIVMSG #chan :text
    if len(parts) < 4 or parts[1] != "PRIVMSG" or not parts[0].startswith(":") or not parts[3].startswith(":"):
        return None
    user = parts[0][1:].partition("!")[0].lower()
    return ChatMessage(now, user, parts[3][1:]) if user else None


def parse_kick_frame(raw: str, now: float) -> ChatMessage | None:
    try:
        frame = json.loads(raw)
        if isinstance(frame, dict) and "push" in frame:            # Centrifugo wraps the Pusher-style event
            frame = frame["push"]["pub"]["data"]
        if not isinstance(frame, dict) or frame.get("event") != "App\\Events\\ChatMessageEvent":
            return None
        data = frame["data"]
        data = json.loads(data) if isinstance(data, str) else data
        user, text = data["sender"]["username"], data["content"]
        if not isinstance(user, str) or not isinstance(text, str):
            return None
        return ChatMessage(now, user, " ".join(_KICK_EMOTE.sub(r" \1 ", text).split()))
    except (KeyError, TypeError, ValueError, RecursionError):
        return None


_KICK_EMOTE = re.compile(r"\[emote:\d+:([^\]\s]+)\]")


def _sent_at(usec, now: float) -> float:
    """A YouTube message's own time (it arrives in 10 s batches, 1 to 12 s late); `now` if implausible."""
    try:
        ts = int(usec) / 1e6
    except (TypeError, ValueError, OverflowError):
        return now
    return ts if now - 120 <= ts <= now + 5 else now


def _wait_ms(v) -> int:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    return int(v) if ok else 0


def parse_youtube_page(payload: dict, now: float) -> tuple[list[ChatMessage], str | None, int]:
    try:
        cont = payload["continuationContents"]["liveChatContinuation"]
        token, wait_ms = None, 0
        for item in cont.get("continuations", []):
            for key in ("timedContinuationData", "invalidationContinuationData", "reloadContinuationData"):
                if key in item:
                    token, wait_ms = item[key].get("continuation"), _wait_ms(item[key].get("timeoutMs"))
                    break
            if token:
                break
        actions = cont.get("actions")
    except (KeyError, TypeError, AttributeError, ValueError):
        return [], None, 0
    token = token if isinstance(token, str) else None

    messages = []
    for action in actions if isinstance(actions, list) else []:
        try:
            r = action["addChatItemAction"]["item"]["liveChatTextMessageRenderer"]
            author = r["authorName"]["simpleText"]
            parts = []
            for run in r["message"]["runs"]:
                if "text" in run:
                    parts.append(run["text"])
                elif "emoji" in run:
                    e = run["emoji"]
                    eid = e.get("emojiId", "")
                    # Unicode emoji ids are the emoji itself; custom channel emoji have ASCII ids.
                    parts.append(eid if isinstance(eid, str) and eid and not eid.isascii()
                                 else (e.get("shortcuts") or [""])[0])
            if isinstance(author, str) and all(isinstance(x, str) for x in parts):
                messages.append(ChatMessage(_sent_at(r.get("timestampUsec"), now), author, "".join(parts)))
        except (KeyError, TypeError, AttributeError, IndexError):
            continue
    return messages, token, wait_ms


# Network failures end a source quietly so the caller reconnects; configuration problems raise.
_DROPPED = (OSError, ValueError, WebSocketException, http.client.HTTPException)
TWITCH_IDLE_S = 400                                # Twitch pings about every 5 minutes
KICK_IDLE_S = 60                                   # Kick pings every 25 s
KICK_SESSION_S = 2400                              # reconnect before the 2700 s token expires
MAX_BODY = 5_000_000
_KICK_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
_YOUTUBE_UA = _KICK_UA


def _kick_http(url: str, body: dict | None = None) -> dict:
    headers = {"User-Agent": _KICK_UA, "Accept": "application/json",
               "Content-Type": "application/json", "Origin": "https://kick.com"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read(MAX_BODY))


def _youtube_get(url: str) -> str:
    headers = {"User-Agent": _YOUTUBE_UA, "Accept-Language": "en-US,en;q=0.9",
               "Cookie": "SOCS=CAI; CONSENT=YES+1"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read(MAX_BODY).decode("utf-8", "replace")


def _youtube_post(url: str, body: dict) -> dict:
    headers = {"User-Agent": _YOUTUBE_UA, "Accept-Language": "en-US,en;q=0.9",
               "Cookie": "SOCS=CAI; CONSENT=YES+1", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read(MAX_BODY))


def open_chat(cfg: StreamCfg) -> AsyncIterator[ChatMessage]:
    if cfg.platform not in PLATFORMS:
        raise ValueError(f"unknown platform {cfg.platform!r}")
    return {"twitch": _twitch, "kick": _kick, "youtube": _youtube}[cfg.platform](cfg)


async def _twitch(cfg: StreamCfg) -> AsyncIterator[ChatMessage]:
    try:
        async with websockets.connect("wss://irc-ws.chat.twitch.tv:443") as ws:
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
            await ws.send(f"JOIN #{cfg.channel.lower()}")
            while True:
                raw = await asyncio.wait_for(ws.recv(), TWITCH_IDLE_S)
                ts = time.time()
                messages, pings = [], 0
                for line in raw.split("\r\n"):
                    if line.startswith("PING"):
                        pings += 1
                    else:
                        msg = parse_twitch_line(line, ts)
                        if msg is not None:
                            messages.append(msg)
                for msg in messages:
                    yield msg
                for _ in range(pings):
                    await ws.send("PONG :tmi.twitch.tv")
    except _DROPPED:
        return


async def _kick(cfg: StreamCfg) -> AsyncIterator[ChatMessage]:
    try:
        channel = await asyncio.to_thread(_kick_http, f"https://kick.com/api/v2/channels/{cfg.channel}")
        channel_id, room = channel["id"], channel["chatroom"]["id"]
        client_id = str(uuid.uuid4())
        negotiated = await asyncio.to_thread(
            _kick_http, f"https://web.kick.com/api/v1/realtime/channels/{channel_id}/chat/connection",
            {"client": {"id": client_id, "type": "web"},
             "capabilities": {"accepted_providers": [{"provider": "centrifugo"}]}})
        try:
            conn = negotiated["data"]["connections"][0]
            url = conn["credentials"]["url"]
        except (KeyError, IndexError, TypeError):
            conn = {}
        if conn.get("provider") != "centrifugo":
            raise RuntimeError("kick realtime provider changed")
        auth = await asyncio.to_thread(
            _kick_http, "https://web.kick.com/api/v1/realtime/auth/connection", {"client_id": client_id})
        token = auth["data"]["token"]
        async with websockets.connect(url, origin="https://kick.com", user_agent_header=_KICK_UA) as ws:
            await ws.send(json.dumps({"id": 1, "connect": {"token": token, "name": "js"}}))
            await ws.send(json.dumps({"id": 2, "subscribe": {"channel": f"chatrooms.{room}.v2"}}))
            ends = time.monotonic() + KICK_SESSION_S
            while time.monotonic() < ends:
                raw = await asyncio.wait_for(ws.recv(), KICK_IDLE_S)
                ts = time.time()
                messages, pings = [], 0
                for line in raw.split("\n"):
                    if line.strip() == "{}":
                        pings += 1
                    elif '"error"' in line and _is_error_reply(line):   # connect or subscribe refused
                        return
                    else:
                        msg = parse_kick_frame(line, ts)
                        if msg is not None:
                            messages.append(msg)
                for msg in messages:
                    yield msg
                for _ in range(pings):
                    await ws.send("{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError(f"unknown kick channel {cfg.channel!r}") from None
        return
    except _DROPPED:
        return


def _is_error_reply(line: str) -> bool:
    try:
        reply = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return isinstance(reply, dict) and "id" in reply and "error" in reply


def live_chat_continuation(page: str) -> str | None:
    """The "Live chat" (all messages) token from the sort menu; "Top chat" is a ~3x smaller sample
    that flattens the spikes the detector looks for. Falls back to the page's first token."""
    menu = page[page.find('"subMenuItems"'):] if '"subMenuItems"' in page else ""
    live = re.search(r'"title":"Live chat".*?"continuation":"([^"]+)"', menu[:4000], re.S)
    first = re.search(r'"continuation":"([^"]+)"', page)
    return live.group(1) if live else first.group(1) if first else None


async def _youtube(cfg: StreamCfg) -> AsyncIterator[ChatMessage]:
    try:
        page = await asyncio.to_thread(_youtube_get, cfg.url)
        video = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', page)
        if video is None:
            return
        video_id = video.group(1)
        chat = await asyncio.to_thread(_youtube_get, f"https://www.youtube.com/live_chat?v={video_id}&is_popout=1")
        key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', chat)
        version = re.search(r'"clientVersion":"([^"]+)"', chat)
        initial = live_chat_continuation(chat)
        if not key or not version or not initial:               # chat off or page changed: retry later
            return
        endpoint = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key.group(1)}&prettyPrint=false"
        continuation, stuck = initial, 0
        while True:
            payload = await asyncio.to_thread(_youtube_post, endpoint, {
                "context": {"client": {"clientName": "WEB", "clientVersion": version.group(1)}},
                "continuation": continuation})
            ts = time.time()
            msgs, token, wait = parse_youtube_page(payload, ts)
            stuck = stuck + 1 if token is not None and token == continuation else 0
            if stuck >= 3:                                   # the same page keeps coming back
                return
            if not stuck:                                    # a repeated page would replay its messages
                for msg in msgs:
                    yield msg
            if token is None:
                return
            continuation = token
            await asyncio.sleep(min(wait or 5000, 10000) / 1000)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError(f"unknown youtube channel {cfg.channel!r}") from None
        return
    except _DROPPED:
        return


async def _main(platform: str, channel: str) -> None:
    async for message in open_chat(StreamCfg(platform, channel)):
        print(f"{message.ts:.1f} {message.user}: {message.text}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m autoclipper.chat <platform> <channel>")
    asyncio.run(_main(sys.argv[1], sys.argv[2]))
