"""Which channel URLs are live (streamlink --json). Usage: python tools/probe.py <url> [<url> ...]"""
import json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

def probe(url):
    p = subprocess.run(["streamlink", "--json", url], capture_output=True, text=True, timeout=90)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return f"ERR  {url} | {p.stderr.strip()[:80]}"
    if "error" in d:
        return f"OFF  {url} | {d['error'][:80]}"
    meta = d.get("metadata") or {}
    return f"LIVE {url} | {','.join(list(d['streams'])[:10])} | {str(meta.get('title'))[:50]}"


def main() -> None:
    with ThreadPoolExecutor(6) as ex:
        for line in sorted(ex.map(probe, sys.argv[1:])):
            print(line)


if __name__ == "__main__":
    main()
