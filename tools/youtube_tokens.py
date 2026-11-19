"""Do YouTube chat tokens repeat on quiet polls, and how old are messages? Usage: python tools/youtube_tokens.py <@handle> [seconds]"""
import json, re, sys, time, urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
HDR = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Cookie": "SOCS=CAI; CONSENT=YES+1"}

def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=20) as r:
        return r.read().decode("utf-8", "replace")

def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={**HDR, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main() -> None:
    handle, secs = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60
    page = get(f"https://www.youtube.com/{handle}/live")
    vid = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', page).group(1)
    chat = get(f"https://www.youtube.com/live_chat?v={vid}&is_popout=1")
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', chat).group(1)
    ver = re.search(r'"clientVersion":"([^"]+)"', chat).group(1)
    cont = re.search(r'"continuation":"([^"]+)"', chat).group(1)
    t0 = time.time()
    while time.time() - t0 < secs:
        d = post(f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key}&prettyPrint=false",
                 {"context": {"client": {"clientName": "WEB", "clientVersion": ver}}, "continuation": cont})
        now = time.time()
        lc = d.get("continuationContents", {}).get("liveChatContinuation", {})
        c = (lc.get("continuations") or [{}])[0]; kind = next(iter(c), None); data = c.get(kind, {}) if kind else {}
        new, wait = data.get("continuation"), data.get("timeoutMs")
        ages = []
        for a in lc.get("actions", []):
            r = a.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer")
            if r and r.get("timestampUsec"):
                ages.append(round(now - int(r["timestampUsec"]) / 1e6, 1))
        print(f"t={now - t0:5.1f}s msgs={len(ages):3d} ages(s) min/max={min(ages) if ages else '-'}/{max(ages) if ages else '-'} wait={wait} token_same={new == cont}")
        cont = new
        if not cont: break
        time.sleep(min(wait or 5000, 10000) / 1000)


if __name__ == "__main__":
    main()
