import pytest
from PIL import Image

from autoclipper.render import Word, build_overlay, cues, steps, trim_window


def talk(t0, t1, step=0.3):
    return [Word(round(t0 + i * step, 2), round(t0 + i * step + 0.25, 2), f"w{i}") for i in range(int((t1 - t0) / step))]


def test_trim_starts_after_the_pause_when_cut_mid_sentence():
    words = talk(0.0, 10.0) + talk(10.6, 60.0)          # 0.75 s pause before 10.6
    assert trim_window(words, 80.0, onset_rel=34.0, end_rel=50.0) == (10.6, 58.0)


def test_trim_without_speech_uses_plain_window():
    assert trim_window([], 80.0, onset_rel=34.0, end_rel=50.0) == (9.0, 58.0)


def test_trim_caps_length_and_keeps_the_start():
    assert trim_window([], 110.0, onset_rel=30.0, end_rel=90.0) == (5.0, 64.0)


def test_trim_clamps_to_the_clip():
    assert trim_window([], 40.0, onset_rel=10.0, end_rel=38.0) == (0.0, 40.0)


WORDS = [Word(10.0, 10.2, "he"), Word(10.2, 10.4, "really"), Word(10.4, 10.6, "did"),
         Word(10.6, 10.8, "that"), Word(12.0, 12.3, "bro\n"), Word(25.0, 25.2, "late")]


def test_cues_group_three_words_and_break_on_a_pause():
    got = cues(WORDS, 10.0, 20.0)
    assert [[w.text for w in c] for c in got] == [["HE", "REALLY", "DID"], ["THAT"], ["BRO"]]
    assert got[0][0].start == 0.0 and round(got[2][0].start, 2) == 2.0


def test_steps_highlight_each_word_and_clear_after_a_pause():
    got = steps(cues(WORDS, 10.0, 20.0), 10.0)
    assert [(round(t, 2), c, w) for t, c, w in got] == [
        (0.0, -1, -1), (0.0, 0, 0), (0.2, 0, 1), (0.4, 0, 2), (0.6, 1, 0), (0.8, -1, -1), (2.0, 2, 0), (2.3, -1, -1)]


def test_overlay_files(tmp_path):
    path = build_overlay(WORDS, 10.0, 20.0, "NO WAY", tmp_path)
    lines = path.read_text().splitlines()
    assert lines[0] == "ffconcat version 1.0"
    files = [l for l in lines if l.startswith("file ")]; durs = [float(l.split()[1]) for l in lines if l.startswith("duration ")]
    assert len(files) == len(durs) + 1 and abs(sum(durs) - 10.0) < 0.02 and min(durs) > 0
    first = Image.open(tmp_path / files[0].split("'")[1])
    assert first.size == (1080, 1920) and first.mode == "RGBA"


# --- audit fixes

def test_transcribe_loads_the_model_per_call_and_lets_it_go(tmp_path, monkeypatch):
    import wave
    import faster_whisper
    import autoclipper.render as render
    made = []
    class Model:
        def __init__(self, *a, **k): made.append(self)
        def transcribe(self, audio, **k):
            class W: start, end, word = 0.0, 0.5, " hi"
            class S: words = [W]
            return [S], None
    monkeypatch.setattr(faster_whisper, "WhisperModel", Model)
    def ffmpeg(argv, **kw):
        with wave.open(argv[-1], "wb") as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(b"\0\0" * 160)
    monkeypatch.setattr(render.subprocess, "run", ffmpeg)
    media = tmp_path / "raw.ts"; media.write_bytes(b"x")
    assert render.transcribe(media, "ffmpeg") == [Word(0.0, 0.5, "hi")]
    render.transcribe(media, "ffmpeg")
    assert len(made) == 2                                  # no model kept between clips (500 MB soak limit)

@pytest.mark.parametrize("words,width", [(["X" * 40], 812), (["W" * 30], 748), (["ok", "M" * 30, "ok"], 748)])
def test_fit_never_returns_text_wider_than_its_box(words, width):
    from autoclipper.render import _fit
    font, lines = _fit(words, 92, 48, width, 2)
    space = font.getlength(" ")
    assert max(sum(font.getlength(words[i]) for i in ln) + space * (len(ln) - 1) for ln in lines) <= width
    assert font.size >= 16

def test_overlay_timeline_has_no_drift_from_shared_timestamps(tmp_path):
    words = [Word(1.0, 1.0, "a"), Word(1.0, 1.0, "b"), Word(1.0, 1.2, "c"), Word(1.2, 1.2, "d"), Word(1.2, 1.5, "e")]
    lines = build_overlay(words, 0.0, 4.0, "HOOK", tmp_path).read_text().splitlines()
    durs = [float(l.split()[1]) for l in lines if l.startswith("duration ")]
    assert abs(sum(durs) - 4.0) < 0.001 and min(durs) > 0

@pytest.mark.parametrize("texts", [["I", "REALLY", "DID"], ["UNBELIEVABLE", "OH", "MY"], ["WHAT", "THE", "HECKINGTONSHIRE"]])
def test_highlight_box_never_touches_a_neighbour(texts):
    from autoclipper.render import SUB_BOX, SUB_STROKE, WORD_CLEAR, WORD_PAD, _sub_layout
    font, placed = _sub_layout(texts)
    for (i, x, y), nxt in zip(placed, placed[1:] + [None]):
        w = font.getlength(texts[i])
        assert SUB_BOX[0] <= x - WORD_PAD[0] and x + w + WORD_PAD[0] <= SUB_BOX[1]
        if nxt is not None and nxt[2] == y:                # same line: box edge to the next word's stroke
            assert nxt[1] - SUB_STROKE - (x + w + WORD_PAD[0]) >= WORD_CLEAR
            assert x + w + SUB_STROKE <= nxt[1] - WORD_PAD[0] - WORD_CLEAR


# --- the 16:9 landscape companion

from pathlib import Path
import json
import subprocess

from autoclipper.render import WIDE, extend_window, landscape_thumbnail, render_landscape

FFMPEG = "/opt/homebrew/bin/ffmpeg"


def test_extension_takes_the_setup_and_reaction_up_to_a_pause():
    # a sentence 14.0..19.9 runs into the window at 20.0 (pause before it at 13.0..14.0); after the window the
    # reaction runs 30.0..33.9, then a pause
    words = talk(10.0, 13.0) + talk(14.0, 20.5) + talk(29.0, 34.0) + talk(40.0, 41.0)
    start, end = extend_window(words, 20.0, 30.0, 60.0)
    assert 13.7 <= start < 14.0 and 33.9 <= end <= 34.3                # just before / after a natural pause
    assert extend_window(words, 20.0, 30.0, 60.0, pre=3.0)[0] == 20.0   # no pause within reach: the start stays


def test_extension_never_adds_dead_air():
    assert extend_window([], 20.0, 30.0, 60.0) == (20.0, 30.0)
    far = [Word(15.0, 15.5, "early"), Word(33.0, 33.5, "late")]        # speech, but across a silence of 3 s or more
    assert extend_window(far, 20.0, 30.0, 60.0) == (20.0, 30.0)


def test_extension_is_clamped_to_the_raw_cut():
    words = talk(0.0, 5.2) + talk(44.0, 50.0)
    start, end = extend_window(words, 4.0, 45.0, 50.0)
    assert start == 0.0 and end == 50.0


def test_landscape_overlay_is_full_hd_with_captions_and_no_hook(tmp_path):
    path = build_overlay(WORDS, 9.0, 20.0, "", tmp_path, WIDE)
    files = [l.split("'")[1] for l in path.read_text().splitlines() if l.startswith("file ")]
    blank = Image.open(tmp_path / files[0])
    assert blank.size == (1920, 1080) and blank.getbbox() is None           # no hook card, no frame shadow
    cue = Image.open(tmp_path / files[1])
    left, top, right, bottom = cue.getbbox()
    assert top > 1080 * 0.7 and bottom < 1080 * 0.95                       # captions in the lower band


def _probe(path: Path) -> dict:
    out = subprocess.run([str(Path(FFMPEG).with_name("ffprobe")), "-v", "error", "-show_entries",
                          "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(not Path(FFMPEG).exists(), reason="ffmpeg not installed")
def test_landscape_render_is_16_9_and_runs_the_extended_window(tmp_path):
    raw = tmp_path / "raw.ts"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "30", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(raw)], check=True, timeout=120)
    words = talk(4.0, 7.0) + talk(8.0, 12.2) + talk(20.0, 23.0) + talk(26.0, 27.0)
    short = (12.0, 20.0)
    start, end = extend_window(words, *short, 30.0)
    assert start < short[0] - 3 and end > short[1] + 2                     # setup and reaction both added
    video = render_landscape(raw, start, end, build_overlay(words, start, end, "", tmp_path / "ov", WIDE),
                             tmp_path / "landscape.mp4", FFMPEG)
    info = _probe(video)
    v = next(st for st in info["streams"] if st["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1920, 1080)
    assert any(st["codec_type"] == "audio" for st in info["streams"])
    assert abs(float(info["format"]["duration"]) - (end - start)) < 0.3
    assert float(info["format"]["duration"]) > short[1] - short[0] + 5
    jpg = landscape_thumbnail(raw, 13.0, "NO WAY", tmp_path / "landscape.jpg", FFMPEG)
    assert Image.open(jpg).size == (1280, 720) and jpg.stat().st_size < 2 * 2**20
    assert not (tmp_path / "landscape_hook.png").exists()


# --- caption styles and glyph-safe text

from autoclipper.render import BOX_PAD, SUB_BOX, SUB_CY, TALL, _draw_overlay, _sub_layout, drawable


def _dark(img, x0, x1, y0, y1):      # share of pixels in the box that are dark and mostly opaque
    px = list(img.crop((x0, y0, x1, y1)).getdata())
    return sum(1 for r, g, b, a in px if a > 150 and r + g + b < 120) / len(px)


def test_box_style_puts_a_dark_panel_behind_the_caption(tmp_path):
    cue = [Word(0.0, 0.5, "HELLO"), Word(0.5, 1.0, "THERE")]
    _draw_overlay("", cue, 0, tmp_path / "box.png", style="box")
    _draw_overlay("", cue, 0, tmp_path / "stroke.png", style="stroke")
    box, stroke = (Image.open(tmp_path / n).convert("RGBA") for n in ("box.png", "stroke.png"))
    band = (SUB_BOX[0], SUB_BOX[1], SUB_CY - 60, SUB_CY + 60)
    assert _dark(box, *band) > 1.5 * _dark(stroke, *band)     # the stroke style's black outline is dark too
    font, placed = _sub_layout(["HELLO", "THERE"], TALL, "box")
    x, y = placed[0][1:]
    pad = (int(x) - BOX_PAD[0] + 4, int(x) - 10, int(y) - 30, int(y) - 10)   # the panel's padding before the first word
    assert _dark(box, *pad) > 0.95 and _dark(stroke, *pad) == 0


@pytest.mark.parametrize("layout", [TALL, WIDE])
def test_box_panels_stay_inside_the_caption_box(tmp_path, layout):
    cue = [Word(0.0, 0.5, "UNBELIEVABLE"), Word(0.5, 1.0, "HECKINGTONSHIRE"), Word(1.0, 1.5, "WHAT")]
    _draw_overlay("", cue, 1, tmp_path / "b.png", layout, style="box")
    img = Image.open(tmp_path / "b.png").convert("RGBA")
    w, h = layout.size
    rows = (layout.sub_cy - 120, layout.sub_cy + 120)       # the caption band, clear of the frame shadow
    assert _dark(img, 0, layout.sub_box[0], *rows) == 0 and _dark(img, layout.sub_box[1], w, *rows) == 0
    assert img.getchannel("A").crop((0, 0, w, rows[0])).getextrema()[1] <= 170   # nothing above but the shadow
    assert _dark(img, *layout.sub_box, layout.sub_cy - 40, layout.sub_cy + 40) > 0.3


def test_unknown_caption_style_is_refused(tmp_path):
    with pytest.raises(ValueError):
        build_overlay(WORDS, 10.0, 20.0, "", tmp_path, style="neon")


def test_text_the_font_cannot_draw_is_dropped():
    assert drawable("LET'S GO 💀😭") == "LET'S GO"
    assert drawable("안녕") == ""
    assert drawable("CAFÉ!") == "CAFÉ!"


def test_a_hook_with_emojis_renders_without_tofu(tmp_path):
    _draw_overlay("HE DID IT 😭", [], -1, tmp_path / "h.png")   # must not raise; emoji dropped
    _draw_overlay("HE DID IT", [], -1, tmp_path / "plain.png")
    assert Image.open(tmp_path / "h.png").tobytes() == Image.open(tmp_path / "plain.png").tobytes()


def test_a_word_the_font_cannot_draw_is_skipped_and_the_highlight_stays_on_its_word(tmp_path):
    cue = [Word(0.0, 0.5, "💀"), Word(0.5, 1.0, "HELLO"), Word(1.0, 1.5, "THERE")]
    _draw_overlay("", cue, 1, tmp_path / "a.png", style="box")
    _draw_overlay("", cue[1:], 0, tmp_path / "b.png", style="box")
    assert Image.open(tmp_path / "a.png").tobytes() == Image.open(tmp_path / "b.png").tobytes()


@pytest.mark.parametrize("layout", [TALL, WIDE])
def test_box_words_sit_a_space_apart_not_a_pill_apart(layout):
    words = ["ABOUT", "THE", "CYPHER"]
    def line(style):
        font, placed = _sub_layout(words, layout, style)
        first = [(i, x) for i, x, y in placed if y == placed[0][2]]
        return font, first[-1][1] + font.getlength(words[first[-1][0]]) - first[0][1], first
    (bf, bw, box), (sf, sw, stroke) = line("box"), line("stroke")
    assert bf.size == sf.size and len(box) == len(stroke) > 1 and bw < sw
    gap = box[1][1] - box[0][1] - bf.getlength(words[box[0][0]])
    assert 0.6 * bf.getlength(" ") <= gap < bf.getlength(" ")      # tighter than a bare space, never touching


def test_renders_carry_48_khz_audio(tmp_path):
    raw = tmp_path / "raw.ts"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(raw)], check=True, timeout=120)
    from autoclipper.render import render_short
    words = talk(0.5, 3.0)
    video = render_short(raw, 0.0, 3.5, build_overlay(words, 0.0, 3.5, "HI", tmp_path / "ov"), tmp_path / "s.mp4", FFMPEG)
    out = subprocess.run([str(Path(FFMPEG).with_name("ffprobe")), "-v", "error", "-select_streams", "a",
                          "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "48000"                                   # loudnorm alone resamples to 192 kHz


@pytest.mark.parametrize("start,end,event,expected", [
    (10.0, 50.0, 30.0, (10.0, 50.0)),                       # 40 s: the Short itself
    (10.0, 60.0, 30.0, (10.0, 60.0)),                       # exactly 50 s: still the Short itself
    (10.0, 62.0, 30.0, (12.0, 62.0)),                       # 52 s: as early as fits, ending with the Short
    (10.0, 182.0, 100.0, (95.0, 145.0)),                    # a 172 s Short: 50 s from just before the event
    (10.0, 182.0, 12.0, (10.0, 60.0)),                      # the event near the start: from the Short's start
    (10.0, 182.0, 300.0, (132.0, 182.0)),                   # an event outside the Short: the last 50 s
])
def test_the_reel_window_fits_instagram_and_keeps_the_event(start, end, event, expected):
    from autoclipper.render import REEL_MAX_S, reel_window
    got = reel_window(start, end, event)
    assert got == expected and got[1] - got[0] <= REEL_MAX_S == 50


def test_a_reel_cut_is_encoded_at_5_mbps_with_48_khz_audio(tmp_path, monkeypatch):
    import autoclipper.render as render
    seen = []
    monkeypatch.setattr(render.subprocess, "run", lambda argv, **kw: seen.append(argv))
    render.render_short(tmp_path / "raw.ts", 0.0, 50.0, tmp_path / "subs.ffconcat", tmp_path / "short.mp4", "ffmpeg")
    render.render_reel(tmp_path / "raw.ts", 0.0, 50.0, tmp_path / "subs.ffconcat", tmp_path / "reel.mp4", "ffmpeg")
    short, reel = seen
    rate = lambda argv: argv[argv.index("-b:v") + 1]
    assert rate(short) == "8M" and rate(reel) == "5M" == render.REEL_VIDEO_BITRATE   # about 32 MB for 50 s
    assert reel[reel.index("-ar") + 1] == "48000" and reel[-1].endswith("reel.mp4")
