"""Clipping path only (chat, detector, buffer, raw cut; no render or upload), with the daemon's cut window. Usage: python tools/clip_run.py <minutes> <platform>:<channel> [...]"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from autoclipper.capture import cut, index, pipeline_argv, prune
from autoclipper.chat import open_chat
from autoclipper.config import StreamCfg
from autoclipper.detect import Detector, classify
from autoclipper.run import CUT_DELAY_S, POST_S, PRE_S

FFMPEG = "/opt/homebrew/bin/ffmpeg"
OUT = Path.home() / "autoclipper-data" / "spike" / "cliprun"
LOG = None


def log(**row) -> None:
    row["t"] = round(time.time(), 1)
    LOG.write(json.dumps(row) + "\n")
    LOG.flush()
    print(json.dumps(row), flush=True)


async def stream(cfg: StreamCfg, minutes: float) -> None:
    name = cfg.key.replace(":", "_").replace("@", "")
    buf = OUT / "buf" / name
    run = buf / str(int(time.time()))
    run.mkdir(parents=True)
    sl, ff = pipeline_argv(cfg.url, run, FFMPEG)
    p1 = subprocess.Popen(sl, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen(ff, stdin=p1.stdout, stderr=(run / "ffmpeg.log").open("w"))
    p1.stdout.close()
    det = Detector(cfg.key, lag_s=15 if cfg.platform == "youtube" else 0)
    minute = {"msgs": 0, "reactions": 0}
    cuts = []

    async def chat() -> None:
        delay = 5
        while True:
            try:
                async for m in open_chat(cfg):
                    det.feed(m)
                    minute["msgs"] += 1
                    minute["reactions"] += classify(m.text) is not None
                    delay = 5
                log(stream=name, chat="ended")
            except Exception as e:                  # same rule as the supervisor: chat never stops capture
                log(stream=name, chat="error", error=repr(e)[:200])
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def cut_later(ev, n: int) -> None:
        await asyncio.sleep(CUT_DELAY_S)
        out = OUT / "clips" / f"{name}_{n:02d}.ts"
        clip = await asyncio.to_thread(cut, buf, ev.onset - PRE_S, ev.end + POST_S, out, FFMPEG)   # as the daemon cuts
        log(stream=name, cut=n, ok=clip is not None, file=str(out) if clip else None,
            duration=round(clip.duration, 1) if clip else None, t0=round(clip.t0, 1) if clip else None)

    reader = asyncio.create_task(chat())
    end, n, last_minute = time.time() + minutes * 60, 0, time.time()
    while time.time() < end:
        await asyncio.sleep(1)
        now = time.time()
        ev = det.tick(now)
        if ev:
            n += 1
            log(stream=name, event=n, onset=round(ev.onset, 1), length=round(ev.end - ev.onset, 1),
                peak_z=round(ev.peak_z, 1), score=round(ev.score, 1), mood=ev.mood, sample=ev.sample[:8])
            cuts.append(asyncio.create_task(cut_later(ev, n)))
        if now - last_minute >= 60:
            segs = index(buf)
            log(stream=name, minute=dict(minute), buffered_s=round(sum(s.end - s.start for s in segs)),
                gaps=sum(s.gap_before for s in segs[1:]), pruned=prune(buf, now))
            minute.update(msgs=0, reactions=0)
            last_minute = now
        if p2.poll() is not None:
            log(stream=name, capture="exited", code=p2.returncode)
            break
    reader.cancel()
    await asyncio.gather(*cuts, return_exceptions=True)
    for p in (p1, p2):
        p.terminate()
    log(stream=name, done=True, events=n)


async def main() -> None:
    global LOG
    (OUT / "clips").mkdir(parents=True, exist_ok=True)
    LOG = (OUT / "log.jsonl").open("a")
    minutes = float(sys.argv[1])
    cfgs = [StreamCfg(*arg.split(":", 1)) for arg in sys.argv[2:]]
    await asyncio.gather(*(stream(c, minutes) for c in cfgs))


if __name__ == "__main__":
    asyncio.run(main())
