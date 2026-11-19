import logging
import re
import statistics
from collections import Counter, deque
from dataclasses import dataclass, field

from autoclipper.chat import ChatMessage

WINDOW_S = 10
BASELINE_S = 300
WARMUP_S = 120
Z_OPEN = 2.5                       # round 3: more sensitive, Jev filters what gets through
Z_CLOSE = 1.5
MIN_CHATTERS = 8
MIN_REACTION = 0.25
CLIP_CHATTERS = 5
CLOSE_QUIET_S = 8
MAX_EVENT_S = 60
COOLDOWN_S = 30
NEAR_Z = 2.0                       # seconds at or above this, outside an event, are logged as near misses
CONSENSUS_CHATTERS = 24           # broad reactions can qualify on a smaller increase in overall chat rate
CONSENSUS_REACTION = 0.35

log = logging.getLogger("autoclipper")

LEXICON = {
    "clip": {"clip", "clip it", "clip that", "clipped", "clip this"},
    "funny": {"lol", "lmao", "lmfao", "lul", "lulw", "kekw", "omegalul", "icant", "haha", "hahaha", "💀", "😂", "🤣", "😭"},
    "hype": {"w", "ww", "pog", "pogchamp", "poggers", "letsgo", "lets go", "goat", "hype", "ez",
             "congrats", "congratulations", "grats", "🔥", "🐐", "👑", "👏", "🎉", "🏆"},
    "shock": {"what", "wtf", "omg", "nah", "no way", "bro what", "monkas", "?", "😱", "😳"},
    "fail": {"l", "f", "rip", "sadge", "oof", "unlucky", "😬"},
}


@dataclass(frozen=True)
class Event:
    stream: str
    onset: float
    end: float
    peak_z: float
    score: float
    mood: str
    sample: tuple[str, ...]
    trigger: float | None = None


def _norm(text: str) -> str:
    return re.sub(r"(.)\1{2,}", r"\1", text.lower().strip())


def classify(text: str) -> str | None:
    t = _norm(text)
    words = re.findall(r"[\w']+", t)
    phrase = f" {' '.join(words)} "
    if t.strip("!?., ") in LEXICON["clip"] or any(
        f" {request} " in phrase for request in ("clip it", "clip that", "clip this", "someone clip")
    ):
        return "clip"
    for mood in ("funny", "hype", "shock", "fail"):
        words = LEXICON[mood]
        # Single ASCII letters ("w", "l") match only as whole tokens; emoji also match when doubled or glued.
        if t in words or any(tok in words for tok in t.split()) or any(c in words for c in t if not c.isascii()):
            return mood
    return None


@dataclass
class _Bin:
    second: int
    count: int = 0
    classes: list[str] = field(default_factory=list)
    reactors: set[str] = field(default_factory=set)
    clippers: set[str] = field(default_factory=set)
    messages: list[str] = field(default_factory=list)
    in_event: bool = False


class Detector:
    def __init__(self, stream: str, lag_s: int = 0):
        self.stream = stream
        self.lag_s = lag_s                         # run this far behind so late messages land in their own second
        self.bins: deque[_Bin] = deque(maxlen=BASELINE_S + WINDOW_S)
        self.pending: dict[int, _Bin] = {}
        self.first: int | None = None
        self.last: int | None = None
        self.onset: float | None = None
        self.trigger: float | None = None
        self.peak_z = self.ratio = 0.0
        self.quiet = 0
        self.messages: Counter[str] = Counter()
        self.classes: Counter[str] = Counter()
        self.cooldown_until = float("-inf")

    def feed(self, msg: ChatMessage) -> None:
        b = self.pending.setdefault(int(msg.ts), _Bin(int(msg.ts)))
        b.count += 1
        b.messages.append(_norm(msg.text))
        mood = classify(msg.text)
        if mood:
            b.classes.append(mood)
            b.reactors.add(msg.user)
            if mood == "clip":
                b.clippers.add(msg.user)

    def _take(self, bins) -> None:
        for b in bins:
            b.in_event = True
            self.messages.update(b.messages)
            self.classes.update(b.classes)

    def tick(self, now: float) -> Event | None:
        now -= self.lag_s                          # from here on, all times are delayed message time
        second = int(now) - 1
        if self.last is not None and second <= self.last:          # backwards or repeated tick
            return None
        if self.first is None:
            self.first = second
        start = second if self.last is None else max(self.last + 1, second - self.bins.maxlen + 1)
        new = [self.pending.pop(s, None) or _Bin(s) for s in range(start, second + 1)]
        for k in [k for k in self.pending if k <= second]:         # too old to place
            del self.pending[k]
        self.bins.extend(new)
        self.last = second

        window = [b for b in self.bins if b.second > second - WINDOW_S]
        base = [b.count for b in self.bins if b.second <= second - WINDOW_S and not b.in_event]
        total = sum(b.count for b in window)
        mean = statistics.fmean(base) if base else 0.0
        std = statistics.pstdev(base) if base else 0.0
        z = (total / WINDOW_S - mean) / max(std, 0.2 * mean, 0.3)

        if self.onset is not None:
            self._take(new)
            self.peak_z = max(self.peak_z, z)
            self.quiet = self.quiet + len(new) if z < Z_CLOSE else 0
            if self.quiet < CLOSE_QUIET_S and now - self.onset < MAX_EVENT_S:
                return None
            moods = Counter({k: v for k, v in self.classes.items() if k != "clip"}) or self.classes
            end = min(now - self.quiet, self.onset + MAX_EVENT_S)
            ev = Event(self.stream, self.onset, end, self.peak_z, max(self.peak_z, Z_OPEN) * (1 + self.ratio),
                       moods.most_common(1)[0][0] if moods else "clip",
                       tuple(m for m, _ in self.messages.most_common(30)), self.trigger)
            self.onset, self.quiet, self.cooldown_until = None, 0, now + COOLDOWN_S
            self.messages.clear(); self.classes.clear()
            return ev

        if second - self.first < WARMUP_S or now < self.cooldown_until:
            return None
        ratio = sum(len(b.classes) for b in window) / max(1, total)
        reactors = set().union(*(b.reactors for b in window))
        clippers = set().union(*(b.clippers for b in window))
        spike = z >= Z_OPEN and len(reactors) >= MIN_CHATTERS and ratio >= MIN_REACTION
        consensus = z >= NEAR_Z and len(reactors) >= CONSENSUS_CHATTERS and ratio >= CONSENSUS_REACTION
        requests = len(clippers) >= CLIP_CHATTERS
        if spike or consensus or requests:
            self.trigger = float(second)
            self.onset = _reaction_start(window, second, requests)
            self.peak_z, self.ratio, self.quiet = z, ratio, 0
            self._take(window)
            log.info("%s: event onset %.0f trigger %d z %.2f reactors %d ratio %.2f source %s",
                     self.stream, self.onset, second, z, len(reactors), ratio,
                     "requests" if requests else "spike" if spike else "consensus")
        elif z >= NEAR_Z:
            log.info("near miss %s: z %.2f, reactors %d, ratio %.2f", self.stream, z, len(reactors), ratio)
        return None


def _reaction_start(window: list[_Bin], trigger: int, requests: bool) -> float:
    """Walk back through the current burst, allowing one quiet second between reaction bins."""
    start = trigger
    for bucket in reversed(window):
        if start - bucket.second > 2:
            break
        if requests:
            active = len(bucket.clippers) >= 2
        else:
            active = (len(bucket.reactors) >= 2 and
                      len(bucket.classes) >= MIN_REACTION * bucket.count)
        if active:
            start = bucket.second
    return float(start)
