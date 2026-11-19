from __future__ import annotations

import functools
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"   # a default, kept in one place so it can change
WHITE, INK, HIGHLIGHT = (255, 255, 255, 255), (12, 12, 14, 255), (255, 225, 77, 255)

# Shorts safe areas on 1080x1920: the top bar ends near y=200, the action column covers x>=920 from about y=1000,
# and the title/channel strip starts near y=1600. Layout A puts the 16:9 frame at y 656..1264, so the hook sits in
# the blurred band above it and the subtitles in the band below it.
HOOK_BOX = (90, 990)              # x range of the hook card (top band is clear of the action column)
HOOK_CY = 470                     # vertical centre of the hook card, just above the frame's shadow
HOOK_SIZE, HOOK_MIN = 76, 44      # font px; shrinks to fit at most HOOK_LINES lines
HOOK_LINES = 2
HOOK_PAD = (44, 30)               # card padding x, y
HOOK_RADIUS = 34
SUB_BOX = (60, 900)               # x range of the subtitles, clear of the action column
SUB_CY = 1395                     # vertical centre of the subtitle block, just below the frame's shadow
SUB_SIZE, SUB_MIN = 92, 48
SUB_ONE_LINE_MIN = 74             # a cue shrinks to this size before it wraps onto a second line
SUB_LINES = 2
SUB_STROKE = 8
WORD_PAD = (16, 10)               # highlight box padding around the spoken word
WORD_RADIUS = 18
WORD_CLEAR = 10                   # clear space between the highlight box and the next word's stroke
BOX_FILL = (0, 0, 0, 178)         # "box" caption style: a 70% black rounded panel behind each caption line
BOX_RADIUS = 22
BOX_PAD = (26, 14)                # panel padding x, y around the line's words
BOX_SPACE = 0.8                   # word gap on a panel, in space widths: Arial Black's space (0.34 em) reads loose bare
CAPTION_STYLES = ("box", "stroke")
FIT_FLOOR = 16                    # text keeps shrinking below its minimum size, down to this, rather than overflow
LINE_GAP = 16
SHADOW = (0, 8, 14, 150)          # offset x, offset y, blur radius, alpha
# ponytail: the frame shadow assumes a 16:9 source (1080x608 at y=656); other shapes get a misplaced band.
FRAME_EDGES = (656, 1264)         # top and bottom of the stream frame in layout A
FRAME_SHADOW = (64, 170)          # band height and darkest alpha of the shadow just outside the frame
RENDER_TIMEOUT_S, FFMPEG_TIMEOUT_S = 600, 120

@dataclass(frozen=True)
class Layout:
    size: tuple[int, int]
    hook_box: tuple[int, int]
    hook_cy: int
    hook_size: int
    hook_min: int
    sub_box: tuple[int, int]
    sub_cy: int
    sub_size: int
    sub_min: int
    sub_one_line_min: int
    frame_shadow: bool


TALL = Layout((W, H), HOOK_BOX, HOOK_CY, HOOK_SIZE, HOOK_MIN, SUB_BOX, SUB_CY, SUB_SIZE, SUB_MIN, SUB_ONE_LINE_MIN, True)
# The 16:9 regular video (1920x1080): the stream fills the whole frame, so the captions sit in the lower band like
# broadcast subtitles, above YouTube's bottom 10%. The hook card is drawn only on the thumbnail, in the lower third
# (the duration badge sits bottom right, outside it).
WIDE = Layout((1920, 1080), (200, 1720), 800, 128, 72, (260, 1660), 910, 76, 44, 62, False)
THUMB_SIZE = (1280, 720)          # YouTube's recommended thumbnail size, well under its 2 MB limit

GRAPH = (
    "[0:v]fps=30,split=2[a][b];"
    "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
    "eq=saturation=0.7,colorchannelmixer=rr=0.6:gg=0.6:bb=0.6,scale=1080:1920[bg];"     # 40% darker, less colour
    "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
    "[base][1:v]overlay=0:0:eof_action=pass[v]"
)
_FILL_WIDE = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
WIDE_GRAPH = f"[0:v]fps=30,{_FILL_WIDE}[base];[base][1:v]overlay=0:0:eof_action=pass[v]"
WIDE_THUMB_GRAPH = f"[0:v]{_FILL_WIDE}[base];[base][1:v]overlay=0:0,scale={THUMB_SIZE[0]}:{THUMB_SIZE[1]}[v]"


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


def transcribe(media: Path, ffmpeg: str, *, names: Sequence[str] = ()) -> list[Word]:
    media = Path(media)
    with tempfile.TemporaryDirectory(dir=media.parent) as tmp:
        audio_path = Path(tmp) / "audio.wav"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
             "-sample_fmt", "s16", "-f", "wav", str(audio_path)],
            check=True, timeout=FFMPEG_TIMEOUT_S,
        )
        import numpy as np

        with wave.open(str(audio_path), "rb") as wav:
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    from faster_whisper import WhisperModel

    # Loaded per call and released on return: keeping it would hold well over the 500 MB soak limit all day.
    model = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8", cpu_threads=6)
    segments, _ = model.transcribe(
        audio, language="en", word_timestamps=True, vad_filter=True,
        condition_on_previous_text=False, beam_size=5,
        hotwords=", ".join(name.strip() for name in names if name.strip()) or None,
    )
    result = []
    for segment in segments:                       # a generator: the model works while this runs
        for word in segment.words or []:
            text = word.word.strip()
            if text:
                result.append(Word(float(word.start), float(word.end), text))
    del model
    return result


def trim_window(
    words: Sequence[Word], raw_duration: float, onset_rel: float, end_rel: float,
    pre_roll: float = 25.0, post_roll: float = 8.0, max_len: float = 59.0,
) -> tuple[float, float]:
    start = max(0.0, onset_rel - pre_roll)
    end = min(raw_duration, end_rel + post_roll)
    end = min(end, start + max_len)
    ordered = sorted(words, key=lambda word: word.start)
    prev_word = next((word for word in reversed(ordered) if word.start <= start), None)
    next_word = next((word for word in ordered if word.start > start), None)
    speaking = any(word.start < start < word.end for word in ordered)
    close_words = prev_word is not None and next_word is not None and next_word.start - prev_word.end < 0.4
    if speaking or close_words:
        for index, word in enumerate(ordered):
            if word.start < start or word.start > start + 4.0:
                continue
            previous = ordered[index - 1] if index else None
            if previous is not None and word.start - previous.end >= 0.4:
                start = word.start
                break
    return (round(start, 2), round(end, 2))


def extend_window(words: Sequence[Word], start: float, end: float, raw_duration: float,
                  pre: float = 8.0, post: float = 6.0, max_gap: float = 1.2, pause: float = 0.4) -> tuple[float, float]:
    """The Short's window widened for the regular video: back into the speech just before it (setup) and on into
    the speech just after it (reaction), each by at most `pre` / `post` seconds. It only crosses speech whose gaps
    are under `max_gap` (never dead air) and stops at a pause of at least `pause` (a natural boundary), so with
    nothing said around the window it stays as it is."""
    ordered = sorted(words, key=lambda w: w.start)
    new_start = start
    before = [w for w in ordered if w.start < start]
    edge = start
    for i in range(len(before) - 1, -1, -1):
        w = before[i]
        if w.start < start - pre or edge - w.end > max_gap:
            break
        edge = w.start
        gap = w.start - before[i - 1].end if i else float("inf")     # the first word: nothing said before it
        if gap >= pause:                           # a pause before this word: a clean place to begin
            new_start = max(0.0, w.start - min(0.2, gap / 2))
    new_end = end
    after = [w for w in ordered if w.end > end]
    edge = end
    for i, w in enumerate(after):
        if w.end > end + post or w.start - edge > max_gap:
            break
        edge = w.end
        gap = after[i + 1].start - w.end if i + 1 < len(after) else float("inf")   # the last word
        if gap >= pause:                           # a pause after this word: a clean place to end
            new_end = min(raw_duration, w.end + min(0.3, gap / 2))
    return (round(min(new_start, start), 2), round(max(new_end, end), 2))


def cues(words: Sequence[Word], start: float, end: float) -> list[list[Word]]:
    grouped: list[list[Word]] = []
    for word in words:
        if word.start < start or word.end > end:
            continue
        text = word.text.strip().upper()
        if not text:
            continue
        current = Word(word.start - start, word.end - start, text)
        if not grouped or len(grouped[-1]) >= 3 or current.start - grouped[-1][-1].end > 0.5:
            grouped.append([current])
        else:
            grouped[-1].append(current)
    return grouped


def steps(cue_list: list[list[Word]], duration: float) -> list[tuple[float, int, int]]:
    result: list[tuple[float, int, int]] = [(0.0, -1, -1)]
    for ci, cue in enumerate(cue_list):
        for wi, word in enumerate(cue):
            result.append((word.start, ci, wi))
        next_start = cue_list[ci + 1][0].start if ci + 1 < len(cue_list) else duration
        if next_start - cue[-1].end > 0.5:
            result.append((cue[-1].end, -1, -1))
    return result


@functools.cache
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, size)


def _fit(words: list[str], size: int, smallest: int, width: int, max_lines: int,
         min_gap: float = 0.0, space: float = 1.0) -> tuple[ImageFont.FreeTypeFont, list[list[int]]]:
    """Largest font at which the words wrap greedily into max_lines lines no wider than width. Below `smallest`
    it keeps shrinking (to FIT_FLOOR) only while a line is still too wide: the text may use more lines, never more width."""
    while True:
        font = _font(size)
        gap = max(font.getlength(" ") * space, min_gap)
        lines, line_w = [[]], 0.0
        for i, word in enumerate(words):
            w = font.getlength(word)
            if lines[-1] and line_w + gap + w > width:
                lines.append([])
                line_w = 0.0
            line_w += (gap if lines[-1] else 0) + w
            lines[-1].append(i)
        widest = max(sum(font.getlength(words[i]) for i in ln) + gap * (len(ln) - 1) for ln in lines)
        if (widest <= width and (len(lines) <= max_lines or size <= smallest)) or size <= FIT_FLOOR:
            return font, lines
        size -= 2


def _sub_layout(words: list[str], layout: Layout = TALL,
                style: str = "stroke") -> tuple[ImageFont.FreeTypeFont, list[tuple[int, float, float]]]:
    """Font and (word index, left x, baseline y) for a cue: one line if it fits at the layout's one-line minimum,
    else two. In the stroke style the word gap leaves room for the highlight box and the neighbour's stroke, so they
    never touch; on the box style's panel, with no highlight box to clear, the words sit a little under a space apart."""
    box = layout.sub_box
    if style == "box":
        pad_x, min_gap, space = BOX_PAD[0], 0, BOX_SPACE
    else:
        pad_x, min_gap, space = WORD_PAD[0], WORD_PAD[0] + SUB_STROKE + WORD_CLEAR, 1.0
    width = box[1] - box[0] - 2 * pad_x
    font, lines = _fit(words, layout.sub_size, layout.sub_one_line_min, width, 1, min_gap, space)
    if len(lines) > 1:
        font, lines = _fit(words, layout.sub_size, layout.sub_min, width, SUB_LINES, min_gap, space)
    gap, ascent = max(font.getlength(" ") * space, min_gap), font.getmetrics()[0]
    line_h = ascent + LINE_GAP + WORD_PAD[1]
    y = layout.sub_cy - (len(lines) * line_h - LINE_GAP) / 2 + ascent          # baseline of the first line
    cx = (box[0] + box[1]) / 2
    placed = []
    for ln in lines:
        x = cx - (sum(font.getlength(words[i]) for i in ln) + gap * (len(ln) - 1)) / 2
        for i in ln:
            placed.append((i, x, y))
            x += font.getlength(words[i]) + gap
        y += line_h
    return font, placed


@functools.cache
def _has_glyph(ch: str) -> bool:
    # A missing glyph renders as the font's .notdef box, so compare against that rather than test for ink.
    font = _font(64)
    mask, notdef = font.getmask(ch), font.getmask("\uffff")
    return (mask.size, bytes(mask)) != (notdef.size, bytes(notdef))


def drawable(text: str) -> str:
    """The text without characters the caption font cannot draw (emoji, Korean, ...), so none renders as tofu."""
    return "".join(ch for ch in text if ch.isspace() or _has_glyph(ch)).strip()


@functools.cache
def _frame_shadow() -> Image.Image:
    """Soft dark bands just outside the stream frame's top and bottom edges, so the frame lifts off the background."""
    band, darkest = FRAME_SHADOW
    mask = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(mask)
    top, bottom = FRAME_EDGES
    for k in range(band):
        a = int(darkest * (1 - k / band) ** 2)     # darkest at the edge, fading outwards
        draw.line([(0, top - 1 - k), (W, top - 1 - k)], fill=a)
        draw.line([(0, bottom + k), (W, bottom + k)], fill=a)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    shade.putalpha(mask)
    return shade


def _shadow(layer: Image.Image) -> Image.Image:
    dx, dy, blur, alpha = SHADOW
    mask = layer.getchannel("A").point(lambda a: a * alpha // 255)
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 255), (dx, dy), mask)
    return shadow.filter(ImageFilter.GaussianBlur(blur))


def _draw_overlay(hook: str, cue: list[Word], active: int, out: Path, layout: Layout = TALL,
                  style: str = "stroke") -> None:
    """One transparent frame of the layout's size: the frame shadow (vertical only), the hook card (when given) and,
    when given, a cue with its spoken word marked: boxed in yellow ("stroke") or in yellow text on a dark panel
    per line ("box"). Characters the font cannot draw are dropped, and words left empty are skipped."""
    layer = Image.new("RGBA", layout.size, (0, 0, 0, 0))
    panels = Image.new("RGBA", layout.size, (0, 0, 0, 0))      # drawn without the text's drop shadow
    draw = ImageDraw.Draw(layer)
    hook_words = drawable(hook.upper()).split()
    if hook_words:
        pad_x, pad_y = HOOK_PAD
        box = layout.hook_box
        font, lines = _fit(hook_words, layout.hook_size, layout.hook_min, box[1] - box[0] - 2 * pad_x, HOOK_LINES)
        text = "\n".join(" ".join(hook_words[i] for i in ln) for ln in lines)
        cx = (box[0] + box[1]) / 2
        left, top, right, bottom = draw.multiline_textbbox((cx, layout.hook_cy), text, font=font, anchor="mm",
                                                           align="center", spacing=LINE_GAP)
        draw.rounded_rectangle((left - pad_x, top - pad_y, right + pad_x, bottom + pad_y), HOOK_RADIUS, fill=WHITE)
        draw.multiline_text((cx, layout.hook_cy), text, font=font, fill=INK, anchor="mm", align="center",
                            spacing=LINE_GAP)
    kept = [(i, text) for i, text in enumerate(drawable(w.text) for w in cue) if text]
    active = next((k for k, (i, _) in enumerate(kept) if i == active), -1)
    words = [text for _, text in kept]
    if words and style == "box":
        font, placed = _sub_layout(words, layout, style)
        cap_top = font.getbbox("H", anchor="ls")[1]           # same height for every line, whatever its letters
        panel_draw = ImageDraw.Draw(panels)
        for y in dict.fromkeys(y for _, _, y in placed):
            line = [(i, x) for i, x, ly in placed if ly == y]
            left, right = line[0][1], line[-1][1] + font.getlength(words[line[-1][0]])
            panel_draw.rounded_rectangle((left - BOX_PAD[0], y + cap_top - BOX_PAD[1], right + BOX_PAD[0],
                                          y + BOX_PAD[1]), BOX_RADIUS, fill=BOX_FILL)
            for i, x in line:
                draw.text((x, y), words[i], font=font, fill=HIGHLIGHT if i == active else WHITE, anchor="ls")
    elif words:
        font, placed = _sub_layout(words, layout)
        for i, x, y in placed:
            if i == active:
                pad_x, pad_y = WORD_PAD
                _, top, _, bottom = draw.textbbox((x, y), words[i], font=font, anchor="ls")
                draw.rounded_rectangle((x - pad_x, top - pad_y, x + font.getlength(words[i]) + pad_x, bottom + pad_y),
                                       WORD_RADIUS, fill=HIGHLIGHT)
                draw.text((x, y), words[i], font=font, fill=INK, anchor="ls")
            else:
                draw.text((x, y), words[i], font=font, fill=WHITE, anchor="ls",
                          stroke_width=SUB_STROKE, stroke_fill=INK)
    base = _frame_shadow() if layout.frame_shadow else Image.new("RGBA", layout.size, (0, 0, 0, 0))
    out_img = Image.alpha_composite(Image.alpha_composite(base, panels), _shadow(layer))
    Image.alpha_composite(out_img, layer).save(out)


def build_overlay(words: Sequence[Word], start: float, end: float, hook: str, out_dir: Path,
                  layout: Layout = TALL, style: str = "stroke") -> Path:
    if style not in CAPTION_STYLES:
        raise ValueError(f"unknown caption style {style!r}, expected one of {CAPTION_STYLES}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cue_list = cues(words, start, end)
    timeline: list[tuple[float, int, int]] = []
    for step in steps(cue_list, end - start):      # steps at one timestamp: only the last is ever on screen
        if timeline and abs(timeline[-1][0] - step[0]) < 1e-6:
            timeline[-1] = step
        else:
            timeline.append(step)
    names: dict[tuple[int, int], str] = {}
    for _, ci, wi in timeline:
        key = (ci, wi)
        if key in names:
            continue
        name = "hook.png" if ci < 0 else f"cue_{ci:04d}_{wi:02d}.png"
        names[key] = name
        _draw_overlay(hook, cue_list[ci] if ci >= 0 else [], wi, out_dir / name, layout, style)
    lines = ["ffconcat version 1.0"]
    for index, (time, ci, wi) in enumerate(timeline):
        next_time = timeline[index + 1][0] if index + 1 < len(timeline) else end - start
        duration = max(next_time - time, 0.01)
        lines.extend((f"file '{names[(ci, wi)]}'", f"duration {duration:.3f}"))
    ci, wi = timeline[-1][1:]
    lines.append(f"file '{names[(ci, wi)]}'")
    path = out_dir / "subs.ffconcat"
    path.write_text("\n".join(lines) + "\n")
    return path


def frames(raw: Path, start: float, end: float, n: int, out_dir: Path, ffmpeg: str) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        at = start + (i + 0.5) * (end - start) / n
        path = out_dir / f"frame_{i}.jpg"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", str(at), "-i", str(raw), "-frames:v", "1",
             "-vf", "scale=512:-2", "-q:v", "2", str(path)],
            check=True, timeout=FFMPEG_TIMEOUT_S,
        )
        paths.append(path)
    return paths


REEL_MAX_S = 50.0      # measured 2026-11-17: Meta processed 8 to 52 s Reels and failed every 59 and 60 s one
REEL_LEAD_S = 5.0      # a Reel cut starts this long before the event
REEL_VIDEO_BITRATE = "5M"   # about 32 MB for 50 s: a 51.3 MB Reel failed at Instagram, a 49.3 MB one posted


def reel_window(start: float, end: float, event: float) -> tuple[float, float]:
    """The Instagram Reel's window inside the Short's: the Short itself up to REEL_MAX_S, else REEL_MAX_S starting
    REEL_LEAD_S before the event (as late as needed to stay inside the Short), so the reaction after it is kept."""
    if end - start <= REEL_MAX_S:
        return start, end
    first = min(max(event - REEL_LEAD_S, start), end - REEL_MAX_S)
    return first, first + REEL_MAX_S


def render_short(raw: Path, start: float, end: float, overlay: Path, out: Path, ffmpeg: str) -> Path:
    return _encode(raw, start, end, overlay, out, ffmpeg, GRAPH)


def render_reel(raw: Path, start: float, end: float, overlay: Path, out: Path, ffmpeg: str) -> Path:
    """An Instagram Reel cut: the Short's vertical render at REEL_VIDEO_BITRATE, so it stays well under the size
    Instagram processes."""
    return _encode(raw, start, end, overlay, out, ffmpeg, GRAPH, bitrate=REEL_VIDEO_BITRATE)


def render_landscape(raw: Path, start: float, end: float, overlay: Path, out: Path, ffmpeg: str) -> Path:
    """The regular video: the whole 16:9 stream frame at 1920x1080 with the WIDE captions overlay."""
    return _encode(raw, start, end, overlay, out, ffmpeg, WIDE_GRAPH)


def _encode(raw: Path, start: float, end: float, overlay: Path, out: Path, ffmpeg: str, graph: str, *,
            bitrate: str = "8M") -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-ss", str(start), "-to", str(end), "-i", str(raw),
         "-f", "concat", "-safe", "0", "-i", str(overlay), "-filter_complex", graph,
         "-map", "[v]", "-map", "0:a", "-af", "loudnorm=I=-14:TP=-1.5", "-c:v", "h264_videotoolbox",
         "-b:v", bitrate, "-r", "30", "-c:a", "aac", "-ar", "48000", "-b:a", "160k", "-movflags", "+faststart",
         "-t", str(end - start), str(out)],
        check=True, timeout=RENDER_TIMEOUT_S,
    )
    return out


def thumbnail(raw: Path, at: float, hook: str, out: Path, ffmpeg: str) -> Path:
    return _still(raw, at, hook, out, ffmpeg, TALL, GRAPH)


def landscape_thumbnail(raw: Path, at: float, hook: str, out: Path, ffmpeg: str) -> Path:
    """The regular video's 1280x720 thumbnail: the full frame with the hook card."""
    return _still(raw, at, hook, out, ffmpeg, WIDE, WIDE_THUMB_GRAPH)


def _still(raw: Path, at: float, hook: str, out: Path, ffmpeg: str, layout: Layout, graph: str) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    png = out.with_name(out.stem + "_hook.png")
    _draw_overlay(hook, [], -1, png, layout)
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", str(at), "-i", str(raw), "-loop", "1", "-i", str(png),
             "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-q:v", "2", str(out)],
            check=True, timeout=FFMPEG_TIMEOUT_S,
        )
    finally:
        png.unlink(missing_ok=True)
    return out
