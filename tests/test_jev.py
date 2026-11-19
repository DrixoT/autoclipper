import base64
import json

import pytest
from PIL import Image

from autoclipper import jev
from autoclipper.render import Word


def frame(path, size=(320, 320)):
    Image.new("RGB", size, "red").save(path, format="JPEG")
    return path


def test_build_request_assets_transcript_chat_and_skips(tmp_path):
    one = frame(tmp_path / "one.jpg")
    two = frame(tmp_path / "too-small.jpg", (200, 320))
    words = [Word(0.0, 0.2, str(i)) for i in range(12)] + [
        Word(0.9, 1.1, "pause"), Word(1.2, 1.4, "end")
    ]
    body = jev.build_request(
        "streamer", "game", "hype", 3.6, words,
        [{"s": 0, "n": 0, "top": []}, {"s": 1, "n": 12, "top": ["lol", "w", "kekw", "extra"]}],
        [(1.0, one), (2.0, two)],
    )

    state = body["state"]
    assert body["model"] == jev.MODEL
    assert set(body["questions"]) == {"hook", "context", "payoff", "crowd", "appeal", "story", "dead", "nude"}
    assert state["chat_spike_starts_at_second"] == 4
    assert state["transcript"] == ["[0.0] " + " ".join(str(i) for i in range(12)), "[0.9] pause end"]
    assert state["chat_per_second"] == ["1: 12 msgs | lol, w, kekw"]
    refs = list(state["frames"].values())
    assert len(refs) == len(set(refs)) == len(body["assets"]) == 1
    assert state["frames"].keys() == {"at_1s"}
    assert set(refs) == set(body["assets"])
    asset = body["assets"][refs[0]]
    assert asset["type"] == "image" and asset["format"] == "jpg"
    assert base64.b64decode(asset["data_base64"]).startswith(b"\xff\xd8")


def test_build_request_breaks_lines_on_pause(tmp_path):
    body = jev.build_request("s", "g", "m", 0, [Word(2, 2.2, "one"), Word(3, 3.2, "two")], [], [])
    assert body["state"]["transcript"] == ["[2.0] one", "[3.0] two"]
    assert "assets" not in body


def response():
    answers = {}
    for name, question in jev.QUESTIONS.items():
        if question["type"] == "score":
            answers[name] = {"type": "score", "score": len(question["criteria"]) - 1,
                             "confidence": 0.9,
                             "probabilities": {str(i): 1 / len(question["criteria"])
                                               for i in range(len(question["criteria"]))}}
        else:
            answers[name] = {"type": "noul", "noul": 0.2}
    return {"answers": answers, "usage": {"input_tokens": 100}}


def test_ask_retries_429_honors_retry_after_and_sets_headers():
    calls, waits = [], []

    def post(url, body, headers, timeout=60):
        calls.append((url, json.loads(body), headers))
        if len(calls) == 1:
            return 429, {}, {"Retry-After": "7"}
        return 200, response(), {}

    result = jev.ask({"assets": {"ref": {}}}, "secret-value", post=post, sleep=waits.append)
    assert result["usage"]["input_tokens"] == 100
    assert waits == [7.0]
    assert calls[1][0] == jev.API
    assert calls[1][2] == {
        "Authorization": "Bearer secret-value",
        "Content-Type": "application/json",
        "TypeSafe-Danger-Zone-Images": "true",
    }


def test_ask_gives_up_after_529_retries():
    calls, waits = [], []

    def post(*args):
        calls.append(args)
        return 529, {}, {}

    with pytest.raises(jev.JevError):
        jev.ask({}, "key", post=post, sleep=waits.append)
    assert len(calls) == jev.TRIES
    assert waits == [2, 4, 8]


def test_ask_does_not_retry_400():
    calls = []

    def post(*args):
        calls.append(args)
        return 400, {}, {}

    with pytest.raises(jev.JevError):
        jev.ask({}, "key", post=post, sleep=lambda _: pytest.fail("unexpected retry"))
    assert len(calls) == 1


def test_scores_normalize_and_reject_missing_answer():
    result = jev.scores(response())
    assert result == {"hook": 1.0, "context": 1.0, "payoff": 1.0, "crowd": 1.0, "appeal": 1.0,
                     "story": 1.0, "dead": 0.2, "nude": 0.2}
    partial = response()
    del partial["answers"]["hook"]
    with pytest.raises(jev.JevError):
        jev.scores(partial)


def test_rank_arithmetic():
    assert jev.rank({"hook": 0.8, "context": 0.6, "payoff": 1.0, "crowd": 0.4,
                     "appeal": 0.5, "story": 0.7, "dead": 0.2}) == pytest.approx(0.56)


def test_rank_takes_the_weights_as_an_argument():
    s = {"hook": 0.8, "context": 0.6, "payoff": 1.0, "crowd": 0.4, "appeal": 0.5, "story": 0.7, "dead": 0.2}
    assert jev.rank(s) == jev.rank(s, weights=jev.WEIGHTS)
    only_hook = {"hook": 1, "context": 0, "payoff": 0, "crowd": 0, "appeal": 0, "story": 0}
    assert jev.rank(s, weights=only_hook) == pytest.approx(s["hook"] * (1 - s["dead"]))


def test_rank_includes_story():
    scores = {"hook": 1, "context": 1, "payoff": 1, "crowd": 1, "appeal": 1, "story": 0, "dead": 0}
    assert jev.rank(scores) == pytest.approx(0.80)
    assert set(jev.WEIGHTS) == {"hook", "context", "payoff", "crowd", "appeal", "story"}
    assert sum(jev.WEIGHTS.values()) == pytest.approx(1.0)


def test_api_key_is_read_from_the_keychain_once_found(monkeypatch):
    import autoclipper.jev as jev
    reads = []
    def get(service, name):
        reads.append(name); return None if len(reads) == 1 else "k"
    monkeypatch.setattr("keyring.get_password", get)
    monkeypatch.setattr(jev, "_KEY", [])
    assert jev.api_key() is None and jev.api_key() == "k" and jev.api_key() == "k"
    assert reads == ["typesafe_api_key", "typesafe_api_key"]          # a missing key is asked again, a found one is kept


def test_retry_after_is_clamped_and_a_call_has_a_deadline():
    import autoclipper.jev as jev
    waits, clock = [], [0.0]
    def post(url, body, headers, timeout=60):
        return 429, {}, {"Retry-After": "3600"}
    def sleep(n):
        waits.append(n); clock[0] += n
    with pytest.raises(jev.JevError):
        jev.ask({"model": "m", "state": "s", "questions": {}}, "key", post=post, sleep=sleep, clock=lambda: clock[0])
    assert waits and max(waits) <= 30 and clock[0] <= 90


def test_rank_without_chat_leaves_crowd_out():
    s = {"hook": 0.5, "context": 0.5, "payoff": 0.5, "crowd": 0.0, "appeal": 0.5, "story": 0.5, "dead": 0.0}
    assert jev.rank(s, crowd=False) == pytest.approx(0.5)
    assert jev.rank(s) == pytest.approx(0.45)


def test_nudity_is_never_posted():
    s = {"hook": 1.0, "context": 1.0, "payoff": 1.0, "crowd": 1.0, "appeal": 1.0, "story": 1.0, "dead": 0.0}
    assert jev.rank({**s, "nude": 0.3}) == 0.0
    assert jev.rank({**s, "nude": 0.1}) == pytest.approx(1.0)
    assert jev.rank(s) == pytest.approx(1.0)                 # a rank stored before the question existed
