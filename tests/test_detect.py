import pytest

from autoclipper.chat import ChatMessage
from autoclipper.detect import MAX_EVENT_S, Z_OPEN, Detector, classify


def run(det, script):
    """script: list of (second, [(user, text), ...]); ticks every second, returns events."""
    out, by_sec = [], dict(script)
    for sec in range(max(by_sec) + 80):
        for user, text in by_sec.get(sec, []):
            det.feed(ChatMessage(float(sec), user, text))
        ev = det.tick(float(sec + 1))
        if ev: out.append(ev)
    return out


def steady(seconds, per_sec=5, users=50):
    return [(s, [(f"u{(s * per_sec + i) % users}", "hello chat") for i in range(per_sec)]) for s in seconds]


def burst(seconds, per_sec, text, users=100):
    return [(s, [(f"b{i % users}", text) for i in range(per_sec)]) for s in seconds]


@pytest.mark.parametrize("text,want", [("LMAOOOOO", "funny"), ("WWWWW", "hype"), ("CLIP THAT", "clip"),
    ("💀💀💀", "funny"), ("no way", "shock"), ("hello streamer", None), ("", None)])
def test_classify(text, want):
    assert classify(text) == want


def test_reaction_burst_makes_one_event():
    evs = run(Detector("twitch:a"), steady(range(300)) + burst(range(300, 312), 40, "LMAO"))
    assert len(evs) == 1
    e = evs[0]
    assert e.mood == "funny" and 300 <= e.onset <= 303 and e.end >= 310 and e.peak_z >= 3.0


def test_one_spammer_is_not_an_event():
    evs = run(Detector("twitch:a"), steady(range(300)) + burst(range(300, 310), 40, "LMAO", users=1))
    assert evs == []


def test_burst_without_reactions_is_not_an_event():
    evs = run(Detector("twitch:a"), steady(range(300)) + burst(range(300, 312), 40, "!drop"))
    assert evs == []


def test_no_event_during_warmup():
    evs = run(Detector("twitch:a"), steady(range(60)) + burst(range(60, 72), 40, "LMAO"))
    assert evs == []


def test_clip_requests_open_an_event_without_a_rate_spike():
    script = steady(range(300)) + [(300, [(f"c{i}", "clip it") for i in range(5)])]
    evs = run(Detector("twitch:a"), script)
    assert len(evs) == 1
    assert evs[0].score >= Z_OPEN


def test_second_burst_inside_cooldown_is_ignored():
    script = steady(range(300)) + burst(range(300, 310), 40, "LMAO") + burst(range(330, 340), 40, "LMAO")
    assert len(run(Detector("twitch:a"), script)) == 1


def test_event_is_capped_at_max_length():
    evs = run(Detector("twitch:a"), steady(range(300)) + burst(range(300, 420), 40, "W"))
    assert evs[0].end - evs[0].onset <= MAX_EVENT_S + 1

@pytest.mark.parametrize("text,want", [("😂😂", "funny"), ("LMAO💀", "funny"), ("🔥🔥", "hype"), ("nice play 🔥", "hype"),
                                       ("flow", None), ("awful", None)])
def test_classify_emoji_and_letters(text, want):
    assert classify(text) == want

def warm(det, seconds=300):
    for sec in range(seconds):
        for i in range(5):
            det.feed(ChatMessage(float(sec), f"u{(sec * 5 + i) % 50}", "hello chat"))
        assert det.tick(float(sec + 1)) is None

def test_late_tick_does_not_invent_a_spike():
    det = Detector("twitch:a"); warm(det)
    for i in range(40):
        det.feed(ChatMessage(300.0, f"b{i}", "LMAO"))
    assert det.tick(400.0) is None                       # that burst is 99 s old
    assert all(det.tick(float(t)) is None for t in range(401, 420))

def test_event_end_respects_the_cap_after_a_late_tick():
    det = Detector("twitch:a"); warm(det)
    for sec in range(300, 303):
        for i in range(40):
            det.feed(ChatMessage(float(sec), f"b{i}", "LMAO"))
        assert det.tick(float(sec + 1)) is None
    ev = det.tick(500.0)
    assert ev is not None and ev.end - ev.onset <= MAX_EVENT_S

def test_backwards_tick_is_ignored():
    det = Detector("twitch:a"); warm(det)
    assert det.tick(100.0) is None and det.tick(301.0) is None

def run_delayed(det, script, delay=8):
    """Like run(), but every message is delivered `delay` seconds after it was written."""
    out, by_sec = [], dict(script)
    for wall in range(max(by_sec) + 100):
        for user, text in by_sec.get(wall - delay, []):
            det.feed(ChatMessage(float(wall - delay), user, text))
        ev = det.tick(float(wall + 1))
        if ev: out.append(ev)
    return out

def test_lagged_detector_sees_late_batches():
    script = steady(range(300)) + burst(range(300, 312), 40, "LMAO")
    evs = run_delayed(Detector("youtube:a", lag_s=12), script)
    assert len(evs) == 1 and evs[0].mood == "funny" and 300 <= evs[0].onset <= 303 and evs[0].end <= 325

def test_unlagged_detector_cannot_place_late_messages():
    script = steady(range(300)) + burst(range(300, 312), 40, "LMAO")
    assert run_delayed(Detector("youtube:a"), script) == []


# --- round 3: more sensitive, with near misses logged for calibration

def test_round3_thresholds():
    import autoclipper.detect as d
    assert (d.Z_OPEN, d.MIN_CHATTERS, d.MIN_REACTION, d.COOLDOWN_S) == (2.5, 8, 0.25, 30)

def test_eight_reactors_now_open_an_event():
    evs = run(Detector("twitch:a"), steady(range(300)) + burst(range(300, 312), 40, "LMAO", users=8))
    assert len(evs) == 1

def test_near_misses_are_logged(caplog):
    caplog.set_level("INFO", logger="autoclipper")
    evs = run(Detector("twitch:a"), steady(range(300)) + burst(range(300, 312), 40, "LMAO", users=5))
    misses = [r.getMessage() for r in caplog.records if "near miss" in r.getMessage()]
    assert evs == [] and misses and "twitch:a" in misses[0] and "reactors 5" in misses[0]


def test_many_distinct_reactions_can_open_at_a_smaller_rate_change():
    script = steady(range(300), per_sec=10, users=100)
    for sec in range(300, 308):
        script.append((sec, [(f"reactor-{sec}-{i}", "LMAO") for i in range(7)]
                       + [(f"u{i}", "hello chat") for i in range(8)]))

    event, = run(Detector("twitch:a"), script)
    assert event.peak_z < Z_OPEN
    assert event.onset == 300 and event.trigger == 307


def test_mentions_of_old_clips_are_not_clip_requests():
    script = steady(range(300))
    script += [(300, [(f"talker-{i}", "I saw a clip from yesterday") for i in range(5)])]
    assert run(Detector("twitch:a"), script) == []
    assert classify("someone clip that!") == "clip"
