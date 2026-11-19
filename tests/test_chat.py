import asyncio
import http.client
import json

import pytest

from autoclipper.chat import ChatMessage, live_chat_continuation, open_chat, parse_kick_frame, parse_twitch_line, parse_youtube_page
from autoclipper.config import StreamCfg


def test_twitch_privmsg():
    line = "@badges=;display-name=Foo :foo!foo@foo.tmi.twitch.tv PRIVMSG #kaicenat :LMAOOO no way"
    assert parse_twitch_line(line, 1.0) == ChatMessage(1.0, "foo", "LMAOOO no way")


@pytest.mark.parametrize("line", ["PING :tmi.twitch.tv",
    "@msg-id=sub :tmi.twitch.tv USERNOTICE #kaicenat :thanks", ":tmi.twitch.tv 001 justinfan1 :Welcome"])
def test_twitch_ignores_non_chat(line):
    assert parse_twitch_line(line, 1.0) is None


def test_kick_message_and_ping():
    raw = json.dumps({"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.1.v2",
                      "data": json.dumps({"content": "W", "sender": {"username": "bob"}})})
    assert parse_kick_frame(raw, 2.0) == ChatMessage(2.0, "bob", "W")
    assert parse_kick_frame('{"event":"pusher:ping"}', 2.0) is None
    assert parse_kick_frame("not json", 2.0) is None


def test_youtube_page():
    payload = {"continuationContents": {"liveChatContinuation": {
        "continuations": [{"timedContinuationData": {"continuation": "C2", "timeoutMs": 5000}}],
        "actions": [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                "authorName": {"simpleText": "al"},
                "message": {"runs": [{"text": "no way "}, {"emoji": {"emojiId": "💀", "shortcuts": [":skull:"]}}]}}}}},
            {"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {}}}}]}}}
    assert parse_youtube_page(payload, 3.0) == ([ChatMessage(3.0, "al", "no way 💀")], "C2", 5000)


def test_youtube_page_stream_ended():
    assert parse_youtube_page({}, 3.0) == ([], None, 0)

@pytest.mark.parametrize("cont", [{"actions": None}, {"actions": 7}, {"actions": [7, None, "x"]},
    {"continuations": [{"timedContinuationData": {"continuation": "C", "timeoutMs": float("inf")}}]},
    {"continuations": 5}])
def test_youtube_page_survives_bad_shapes(cont):
    msgs, token, wait = parse_youtube_page({"continuationContents": {"liveChatContinuation": cont}}, 1.0)
    assert msgs == [] and isinstance(wait, int)

def test_youtube_page_skips_non_string_author():
    item = {"liveChatTextMessageRenderer": {"authorName": {"simpleText": []}, "message": {"runs": [{"text": "hi"}]}}}
    payload = {"continuationContents": {"liveChatContinuation": {"actions": [{"addChatItemAction": {"item": item}}]}}}
    assert parse_youtube_page(payload, 1.0)[0] == []

def test_kick_deeply_nested_json_returns_none():
    assert parse_kick_frame("[" * 10000 + "]" * 10000, 1.0) is None

def test_kick_centrifugo_push():
    inner = {"event": "App\\Events\\ChatMessageEvent", "data": json.dumps({"content": "W", "sender": {"username": "bob"}})}
    raw = json.dumps({"push": {"channel": "chatrooms.1.v2", "pub": {"data": inner}}})
    assert parse_kick_frame(raw, 2.0) == ChatMessage(2.0, "bob", "W")
    assert parse_kick_frame("{}", 2.0) is None
    assert parse_kick_frame('{"id":2,"subscribe":{}}', 2.0) is None
    other = {"push": {"pub": {"data": {"event": "App\\Events\\UserBannedEvent", "data": "{}"}}}}
    assert parse_kick_frame(json.dumps(other), 2.0) is None

def test_kick_emote_codes_become_words():
    raw = json.dumps({"event": "App\\Events\\ChatMessageEvent",
                      "data": json.dumps({"content": "[emote:37226:KEKW][emote:37226:KEKW] lol", "sender": {"username": "bob"}})})
    assert parse_kick_frame(raw, 2.0) == ChatMessage(2.0, "bob", "KEKW KEKW lol")



class FakeSocket:
    """One websocket: hands out the given frames, records what was sent, then closes."""
    def __init__(self, *frames, hang=False):
        self.frames, self.sent, self.hang = list(frames), [], hang
    async def send(self, value):
        self.sent.append(value)
    async def recv(self):
        if not self.frames:
            if self.hang:                                 # an open socket that never sends again
                await asyncio.Event().wait()
            raise OSError("closed")
        return self.frames.pop(0)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None


def collect(cfg):
    async def go():
        return [m async for m in open_chat(cfg)]
    return asyncio.run(asyncio.wait_for(go(), 5))        # a source that never ends fails the test instead of hanging


def test_twitch_source(monkeypatch):
    ws = FakeSocket("PING :tmi.twitch.tv\r\n:a!a@a PRIVMSG #x :LUL\r\n:b!b@b PRIVMSG #x :W")
    monkeypatch.setattr("autoclipper.chat.websockets.connect", lambda *a, **k: ws)
    assert [m.user for m in collect(StreamCfg("twitch", "X"))] == ["a", "b"]
    assert "PONG :tmi.twitch.tv" in ws.sent and "JOIN #x" in ws.sent


def test_kick_source(monkeypatch):
    inner = {"event": "App\\Events\\ChatMessageEvent", "data": json.dumps({"content": "W", "sender": {"username": "bob"}})}
    ws = FakeSocket("{}\n" + json.dumps({"push": {"channel": "chatrooms.1.v2", "pub": {"data": inner}}}))
    provider = {"name": "centrifugo"}
    def http(url, body=None):
        if url.endswith("/channels/slug"):
            return {"id": 4, "chatroom": {"id": 1}}
        if url.endswith("/chat/connection"):
            return {"data": {"connections": [{"provider": provider["name"], "credentials": {"url": "wss://kick"}}]}}
        return {"data": {"token": "tok"}}
    monkeypatch.setattr("autoclipper.chat._kick_http", http)
    monkeypatch.setattr("autoclipper.chat.websockets.connect", lambda *a, **k: ws)
    assert [(m.user, m.text) for m in collect(StreamCfg("kick", "slug"))] == [("bob", "W")]
    assert "{}" in ws.sent and '"chatrooms.1.v2"' in ws.sent[1]
    provider["name"] = "pusher"
    with pytest.raises(RuntimeError, match="kick realtime provider changed"):
        collect(StreamCfg("kick", "slug"))


def test_youtube_source(monkeypatch):
    page = '<link rel="canonical" href="https://www.youtube.com/watch?v=abcdefghijk">'
    chat_page = '"INNERTUBE_API_KEY":"key" "clientVersion":"1" "continuation":"C1"'
    message = {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
        "authorName": {"simpleText": "al"}, "message": {"runs": [{"text": "hi"}]}}}}}
    polls = iter([
        {"continuationContents": {"liveChatContinuation": {
            "continuations": [{"timedContinuationData": {"continuation": "C2", "timeoutMs": 60000}}],
            "actions": [message]}}},
        {"continuationContents": {"liveChatContinuation": {"actions": []}}},
    ])
    monkeypatch.setattr("autoclipper.chat._youtube_get", lambda url: page if url.endswith("/live") else chat_page)
    monkeypatch.setattr("autoclipper.chat._youtube_post", lambda url, body: next(polls))
    sleeps = []
    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr("autoclipper.chat.asyncio.sleep", fake_sleep)
    assert [(m.user, m.text) for m in collect(StreamCfg("youtube", "@channel"))] == [("al", "hi")]
    assert sleeps == [10.0]


KICK_CHAT = json.dumps({"push": {"channel": "chatrooms.1.v2", "pub": {"data": {"event": "App\\Events\\ChatMessageEvent",
                        "data": json.dumps({"content": "W", "sender": {"username": "bob"}})}}}})


def kick_http(url, body=None):
    if url.endswith("/channels/slug"):
        return {"id": 4, "chatroom": {"id": 1}}
    if url.endswith("/chat/connection"):
        return {"data": {"connections": [{"provider": "centrifugo", "credentials": {"url": "wss://kick"}}]}}
    return {"data": {"token": "tok"}}


def test_twitch_source_ends_when_idle(monkeypatch):
    monkeypatch.setattr("autoclipper.chat.TWITCH_IDLE_S", 0.05)
    monkeypatch.setattr("autoclipper.chat.websockets.connect", lambda *a, **k: FakeSocket(hang=True))
    assert collect(StreamCfg("twitch", "x")) == []


def test_kick_source_ends_on_an_error_reply(monkeypatch):
    monkeypatch.setattr("autoclipper.chat.KICK_IDLE_S", 30)
    ws = FakeSocket('{"id":2,"error":{"code":103,"message":"permission denied"}}', hang=True)
    monkeypatch.setattr("autoclipper.chat._kick_http", kick_http)
    monkeypatch.setattr("autoclipper.chat.websockets.connect", lambda *a, **k: ws)
    assert collect(StreamCfg("kick", "slug")) == []


def test_kick_source_ends_when_the_session_is_old(monkeypatch):
    monkeypatch.setattr("autoclipper.chat.KICK_SESSION_S", 0)
    ws = FakeSocket(*[KICK_CHAT] * 50, hang=True)
    monkeypatch.setattr("autoclipper.chat._kick_http", kick_http)
    monkeypatch.setattr("autoclipper.chat.websockets.connect", lambda *a, **k: ws)
    assert len(collect(StreamCfg("kick", "slug"))) <= 1


def test_youtube_source_ends_on_a_stuck_page(monkeypatch):
    page = '<link rel="canonical" href="https://www.youtube.com/watch?v=abcdefghijk">'
    chat_page = '"INNERTUBE_API_KEY":"key" "clientVersion":"1" "continuation":"C0"'
    message = {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
        "authorName": {"simpleText": "al"}, "message": {"runs": [{"text": "hi"}]}}}}}
    posted = []
    def post(url, body):
        posted.append(body["continuation"])
        return {"continuationContents": {"liveChatContinuation": {
            "continuations": [{"invalidationContinuationData": {"continuation": "C1", "timeoutMs": 1000}}],
            "actions": [message]}}}
    async def no_sleep(seconds):
        return None
    monkeypatch.setattr("autoclipper.chat._youtube_get", lambda url: page if url.endswith("/live") else chat_page)
    monkeypatch.setattr("autoclipper.chat._youtube_post", post)
    monkeypatch.setattr("autoclipper.chat.asyncio.sleep", no_sleep)
    assert [m.text for m in collect(StreamCfg("youtube", "@channel"))] == ["hi"]
    assert posted == ["C0", "C1", "C1", "C1"]


def test_truncated_http_responses_end_the_source(monkeypatch):
    def truncated(*a, **k):
        raise http.client.IncompleteRead(b"")
    monkeypatch.setattr("autoclipper.chat._kick_http", truncated)
    assert collect(StreamCfg("kick", "slug")) == []
    monkeypatch.setattr("autoclipper.chat._youtube_get", truncated)
    assert collect(StreamCfg("youtube", "@channel")) == []


def test_youtube_message_uses_its_own_timestamp():
    def action(text, usec):
        r = {"authorName": {"simpleText": "al"}, "message": {"runs": [{"text": text}]}}
        if usec is not None:
            r["timestampUsec"] = usec
        return {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": r}}}
    payload = {"continuationContents": {"liveChatContinuation": {"actions": [
        action("a", "993500000"), action("b", None), action("c", "5000000"), action("d", "junk")]}}}
    msgs = parse_youtube_page(payload, 1000.0)[0]
    assert [(m.text, m.ts) for m in msgs] == [("a", 993.5), ("b", 1000.0), ("c", 1000.0), ("d", 1000.0)]


def test_youtube_infinite_timestamp_falls_back_to_now():
    r = {"authorName": {"simpleText": "al"}, "message": {"runs": [{"text": "a"}]}, "timestampUsec": float("inf")}
    payload = {"continuationContents": {"liveChatContinuation": {"actions": [
        {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": r}}}]}}}
    assert [m.ts for m in parse_youtube_page(payload, 1000.0)[0]] == [1000.0]


def test_live_chat_continuation_prefers_all_messages():
    page = ('"continuation":"FIRST" "subMenuItems":[{"title":"Top chat","continuation":{"reloadContinuationData":'
            '{"continuation":"TOP"}}},{"title":"Live chat","continuation":{"reloadContinuationData":{"continuation":"ALL"}}}]')
    assert live_chat_continuation(page) == "ALL"
    assert live_chat_continuation('"continuation":"FIRST"') == "FIRST"
    assert live_chat_continuation("no chat") is None
