# autoclipper design

**What it is for:** while up to five streamers are live, the best moment their chat reacted to is on your YouTube
channel as a subtitled Short within minutes, with nobody at the keyboard.

This document describes how the pipeline works today and why. Where it gives a number, the number comes from the code
(the constant is named so you can find it).

## Goals and constraints

- Sources: Twitch, Kick and YouTube. At most five streams at once (`config.MAX_STREAMS`).
- Fully automatic. It runs until it is stopped, and a run of 48 hours or more must not drain any model quota.
- One Mac, one process, state in SQLite. No server, no web dashboard.
- Official APIs first. Browser automation only as a fallback.

## The key choices

**No LLM watches chat or video.** Watching five streams with a model would cost far more than any quota allows and
would make the pipeline depend on a model being up. Capture, chat reading, detection, transcription, the quality check
and rendering are local code. Models only judge a short list of candidates that local code already found.

**Two small model calls, each with one job.** TypeSafe's Jev scores each candidate against fixed questions and returns
numbers (cheap, typed, no free text to parse). Claude Haiku, through the `claude` CLI with no tools, is the editor:
one call per clip it is asked about, at most 100 a day (`meta.PROVIDERS`). It picks the window and writes the title,
hook and description. Chat and transcript text is untrusted, so nothing that can run a command ever sees it, and
every public field the editor returns is checked again by code.

**Official APIs first, Studio as the fallback.** Uploads use the YouTube Data API. YouTube's terms restrict automated
access to its site, and Studio's page changes without notice, so Studio in a signed-in Chrome profile is used only when
the API's quota is gone or it refuses a Short's thumbnail. Instagram uses Meta's content publishing API.

**Never post twice.** A clip that was mid-upload when the process stopped is marked failed at restart and never
retried. The same holds for the 16:9 companion and the Reel. Losing a post is better than posting a duplicate.

**Learn by fitting a few bounded numbers, not by letting a model rewrite itself.** See "Learning loop" below.

## Pipeline

```
discover (Twitch Helix, Kick) -> up to 5 streams
per stream:  streamlink | ffmpeg -> numbered 4 s .ts segments, 10-minute ring        (capture.py)
             chat reader, no login -> ChatMessage(ts, user, text)                    (chat.py)
             detector, once a second -> Event                                       (detect.py)
on Event:    raw cut copied from the ring (no encode)                               (run.py, capture.py)
             frozen / silent check -> Jev scores -> rank                            (quality.py, jev.py)
each slot, or at once in the fast lane:
             faster-whisper words -> editor (Haiku) -> render 9:16, + 16:9           (render.py, meta.py)
             -> YouTube Data API (Studio fallback) -> Instagram Reel for public Shorts  (upload.py, studio.py, instagram.py)
guards:      daily cap, gap between posts, strike guard, disk guard
learning:    record every candidate and post, refit weights, bars and examples       (learn.py)
```

### Stream discovery

With `[discover]`, the daemon refreshes the stream list every `refresh_minutes`. Pinned streamers take a slot while
live, whatever they play. Preferred streamers come next, in any game. The rest of the five slots go to the biggest
live streams in the configured game, language and viewer floor, on Twitch (Helix API, needs an app client ID and
secret) and Kick (its public endpoints). YouTube is not discovered, because search costs too much quota; pin a YouTube
stream instead.

A discovered streamer must allow clipping: someone has to have made a clip of them in the last seven days
(`discover.CLIP_WINDOW_S`). That is a signal that clipping is on and open to viewers. It is not permission to repost.

### Capture

Streamlink pipes each stream into ffmpeg, which writes numbered 4-second segments (`capture.SEG_S`) and a `list.csv`
with each file's start and end in stream time. Segments are numbered, not named by the clock: platforms deliver video
in bursts, and clock names made ffmpeg overwrite part of the footage. Wall-clock time is stream time plus the smallest
arrival delay seen over the next few files (`capture.ANCHOR_WINDOW`), which removes the burst jitter. The ring keeps
10 minutes (`capture.KEEP_S`). Each capture process gets its own directory, and each stream task restarts itself with a
5 to 60 second backoff (`run.BACKOFF`).

Capture pauses instead of filling a disk. When `volume` is set, it pauses while that volume is unmounted or has under
5 GB free; otherwise the check is free space on `data_dir`'s disk.

### Chat

All three readers are unauthenticated and read-only, behind one interface:

- Twitch: IRC over websocket as an anonymous `justinfan<n>` user.
- Kick: the site's own websocket, negotiated the way the web page does it. Unofficial.
- YouTube: the live video is resolved from `https://www.youtube.com/@handle/live`, then the site's live chat endpoint
  is polled with the continuation token, reading the full "Live chat" feed rather than "Top chat". Unofficial.

The unofficial readers can break without notice. The stream then keeps recording but detects nothing, and `status`
shows how long ago chat was last seen.

### Detection

Detection is statistics and a lexicon, not a model. Each message is classified as `funny`, `hype`, `shock`, `fail`,
`clip` (a request for a clip) or nothing, after collapsing repeated letters. Every second, per stream:

- `rate` is messages per second over the last 10 s (`WINDOW_S`); the baseline is the mean and standard deviation over
  the previous 300 s (`BASELINE_S`), skipping seconds inside an event.
- `z = (rate - mean) / max(std, 0.2 * mean, 0.3)`.
- An event opens when `z >= 2.5` (`Z_OPEN`), at least 8 different chatters reacted (`MIN_CHATTERS`) and at least 25% of
  the window's messages matched (`MIN_REACTION`); or on a broad consensus (24 reactors, 35% reactions) with a smaller
  rise in rate; or when 5 different chatters asked for a clip (`CLIP_CHATTERS`). Counting chatters, not messages,
  keeps one spammer or bot from opening an event.
- It closes after 8 quiet seconds (`z < 1.5`) or at 60 s, then a 30 s cooldown. Nothing fires in the first 120 s.
- The event carries a score, a mood (its most common class) and a sample of messages.

The detector is tuned to be sensitive. Jev and the editor filter what gets through, which is cheaper than missing a
moment.

### The raw cut

Chat reacts after viewers see the moment, so the moment sits before the spike. When an event closes, the covering
segments are copied, without re-encoding, into `clips/<id>/raw.ts`. With story editing on (the default) the cut runs
from 240 s before the onset to 60 s after the end (`run.STORY_PRE_S`, `STORY_POST_S`); for short moments, 90 s before
and 30 s after (`PRE_S`, `POST_S`). If the ring has a gap (a Twitch ad, a reconnect), the cut shrinks to the
footage around the event that is there, or the event is dropped.

### Quality gate

Before any model sees a cut, ffmpeg's `freezedetect` and `silencedetect` measure how much of it is frozen or silent. A
cut more than half frozen or half silent is dropped (`run.MAX_FROZEN`, `MAX_SILENT`).

### Jev ranking

Jev gets the transcript, the per-second chat timeline, four frames and the stream's game, and answers fixed questions
(`jev.QUESTIONS`): `hook`, `context`, `payoff`, `crowd`, `appeal` and `story` as scores, `dead` (mostly dead air, menus
or ads) and `nude` as probabilities. The rank is the weighted mean of the six scores (`jev.WEIGHTS`), scaled down by
`dead`. A clip cut from a recording has no chat, so `crowd` is left out. A `nude` answer of 0.3 or more ranks the clip
0, and it is never posted (`jev.NUDE_MAX`): YouTube flags nudity in a game as well.

Without a TypeSafe key the detector's score ranks instead, and nothing screens the frames for nudity. With a key,
a slot where Jev does not answer (within 90 s) posts nothing: its candidates wait for the next pass, because Jev's
frame check is the only nudity gate.

### Choosing what to post

Each slot (`slot_minutes`) posts the best candidate at or above the slot minimum rank (`jev.MIN_RANK`, 0.45, unless
the learning loop moved it). A candidate at or above `fast_lane` is posted at once, without waiting for the slot. Every
post on a channel waits at least `min_gap_minutes` after the previous one, and no channel goes over
`max_uploads_per_day`. Candidates live for two slots at most.

### The editor and metadata

faster-whisper (`large-v3-turbo`, int8, CPU, with its voice-activity filter) gives word times. The editor gets the
transcript with times, the per-second chat timeline, the mood, the streamer, the platform and the game, and returns
strict JSON: `keep`, `what_happened`, the window, `title`, `hook`, `description`, `tags` and up to three emojis from a
fixed list.

- With story editing (`narrative = true`), it chooses a 60 to 300 s story with the setup, the event and the reaction,
  marks the event, and chooses a 30 to 180 s Short inside the story. A cut that lacks any part of the story is dropped.
  If the editor cannot be asked (its daily cap, or the CLI is missing) the clip is dropped: a story needs judgement.
- With `narrative = false`, it chooses a 10 to 180 s window. If it cannot be asked, a rule-based trim and a template
  write the clip instead, so the pipeline still posts with no model at all.

Every public field is checked for length, control characters, `<` and `>`, links and @mentions. Game tags are added by
code, so the model cannot leave them out. The prompt includes a few example edits: hand-picked at first, later the
channel's best posts (see "Learning loop").

### Rendering

Vertical 1080x1920, H.264 through VideoToolbox, audio normalised to -14 LUFS at 48 kHz. The stream frame sits in the
middle, a hook card above it and word-by-word captions below it. Captions and the hook are transparent PNGs drawn with
Pillow, one per word step with the current word highlighted, and overlaid by ffmpeg, so no ffmpeg built with text
filters is needed.

Two presentation variants alternate so their results can be compared (`caption_variants`): `box` puts the captions on
a dark panel and keeps the title plain; `emoji` uses stroked captions and appends the editor's emojis to the title.
Each clip gets the variant with fewer posts.

With `landscape = true`, the story (or the Short's window, widened into the speech around it) is also rendered as a
16:9 video: the full frame at 1920x1080, never cropped, captions in the lower band, and its own 1280x720 thumbnail. It
reuses the transcript and the editor's metadata, so it costs no model call.

### Upload

- YouTube Data API `videos.insert` (resumable), OAuth for an installed app, then `thumbnails.set` once the video has
  finished processing. Scopes: `youtube.upload` and `youtube.readonly` (the strike check lists videos), plus
  `yt-analytics.readonly` only for a profile signed in with `--analytics`.
- The description is the editor's text followed by a fixed credit line with the streamer's name and stream URL.
- On an exhausted API quota, or a refused Short thumbnail, the upload or thumbnail goes through YouTube Studio in the
  Chrome profile signed in to that channel. Studio only opens the channel the API last verified for the profile.
- A channel upload-limit error pauses that channel for 3 hours (`run.LIMIT_PAUSE_S`). A failed sign-in pauses it until
  `resume`.
- **Strike guard:** every 30 minutes the recent uploads are checked. If one was rejected or has disappeared, that
  channel's uploads pause and a macOS notification is shown. Capture carries on. Uploads restart only with `resume`.
- The 16:9 companion is queued before its Short uploads and posted once the gap allows, without `#shorts`, on the same
  profile and privacy. A failed companion never touches its Short.

### Instagram

A separate queue posts each public Short as a Reel through Meta's content publishing API, after YouTube reports the
Short as public. The caption credits the original stream; the cover is the event frame. Meta fails Reels near a minute
long, so a Short over 50 s gets its own 50 s Reel cut starting 5 s before the event (`render.REEL_MAX_S`), encoded at
5 Mbps to stay under Meta's file size limit. A Reel whose publication is uncertain is held for inspection instead of
being retried.

### Staying up

- launchd keeps the process alive, and the daemon holds a `caffeinate -w <pid>` child so the Mac does not sleep.
- One heavy job at a time (preparing, scoring, rendering, uploading a clip) under one lock.
- Finished clip folders are removed after 72 hours. Each keeps `context.json` (game, display name, chat timeline),
  `analysis.json` (transcript, frames, frozen and silent shares) and, with uploads off or `landscape = true`,
  `meta.json` with everything that was or would be posted.

## Learning loop

Every ranking weight, posting bar and editor example starts as a hand-set value. The learning loop moves them from the
pipeline's own results, inside hard limits, and logs every change. It makes no model call and never stops or delays a
post: every call into it from the posting path is wrapped, and on any error the hand-set values apply.

**The record** (`learn_db`, default `<data_dir>/learning.db`) holds every scored candidate with its Jev answers, the
editor's verdict, and each public post with its views and likes 1, 6, 24 and 72 hours after posting
(`learn.AGES_H`). A snapshot missed by more than a day is skipped, not taken late. Reading statistics never goes
through the code path that can pause a channel. When the editor could not be asked, no verdict is recorded, so a day
at the editor's cap adds no false rejections.

**The label.** Each candidate the editor saw gets `y` in 0 to 1: 0 when the editor dropped it; `0.4 + 0.6 × p` once
it has a 24-hour result, where `p` is its percentile by views among recent posts on the same channel and stream (or the
same channel, when the stream has fewer than 5 results); 0.7 while it is kept but has no usable result. Comparing
within a stream keeps streamer size out of the label, so the loop cannot learn "clip bigger streamers". When YouTube
Analytics is allowed, `p` also averages in how much of the video people watched.

**Ranking weights.** Fitted at most once a day, with at least 30 labelled candidates (8 dropped, 8 kept). Two
least-squares fits are compared: `y` on the current rank alone, and on the six answers freely. The weights move only
when the free fit wins clearly (F statistic of at least 3.5, about a 1-in-100 chance on noise). They then step toward
the free fit's non-negative weights, at most 0.03 each, staying between 0.05 and 0.40 and summing to 1. On simulated
records, an ungated fit moved the weights on almost every day of pure noise; the gated one moved them on under 1%.

**Posting bars.** From the last 24 hours of candidates (at least 30), re-ranked with the current weights, the loop
estimates how many candidates the editor must see to fill the daily cap, given how many it keeps, capped at 80 so the
editor stays under its 100 calls. The slot minimum becomes the rank quantile that lets that many through, between 0.35
and 0.55; the post-at-once rank sits above it, at most 0.80. On a busy day the bars rise and the cap goes to the day's
best; on a quiet day they fall to their floors.

**Exploration.** After every 9 normal public posts, and at most once an hour, one slot may try the best candidate up to
0.10 below the slot minimum. It goes after the slot's normal candidates, so it only takes a slot none of them
filled, and the editor still has to keep it. Without
this, the loop would only ever see candidates above the bar and could not tell whether the bar is too high.

**Editor examples.** Once 10 posts have results and at least 2 are in the top fifth of their group, the three best
replace the hand-picked examples in the editor's prompt, built from the editor's own stored sentences and cut to 200
characters each. They are labelled in the prompt as data, never instructions.

**Control.** `learn = false` keeps recording but applies only hand-set values. `autoclipper learn` shows the counts,
the weights against the hand-set ones, both bars, the examples in use and the last changes; `--freeze`, `--unfreeze`
and `--reset` control it.

Rejected approaches: letting an LLM read the results and rewrite its own rules or prompt (it drifts, cannot be tested,
spends quota and turns untrusted text into instructions), and training a model on clip features (it needs weeks of
posts before it beats the constants).

## Limits

- **Copyright and reuse.** The source is other creators' streams. Streams with music or sports draw claims, and three
  strikes end a channel. The strike guard limits the damage but cannot prevent the first strike. Reused-content rules
  can limit a clip channel's reach and monetisation even with permission. See "Responsible use" in the README.
- **Unofficial endpoints.** Kick chat, YouTube chat and Kick discovery use the sites' own endpoints and can break
  without notice.
- **Studio automation** may trip Google's bot checks, which is why it is only the fallback.
- **The channel upload limit** is not published by YouTube and depends on the channel's history.
- **The learning loop learns slowly.** Only candidates the editor saw have a label, view counts are noisy, and with a
  handful of posts the label is mostly the editor's verdict. The bounds, the daily step and exploration keep a thin or
  biased sample from moving things far.
- **Detection is not learned.** The detector's thresholds and lexicon are fixed; the later stages filter cheaply.
- **macOS only.** launchd, the Keychain, VideoToolbox and the system font are assumed.

## Out of scope

TikTok, a web dashboard, a vision model picking moments, face-tracked reframing, and scheduling beyond "run until
stopped".
