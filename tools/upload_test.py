"""One private 10 s vertical test upload through the API, then its thumbnail. Usage: python tools/upload_test.py <dir with test.mp4 and thumb.jpg>"""
import sys
import time
from pathlib import Path

from autoclipper.meta import Meta
from autoclipper.upload import client, set_thumbnail, upload_short


def main() -> None:
    d = Path(sys.argv[1])
    yt, _ = client()
    meta = Meta("Autoclipper upload test", "TEST", "A private test upload to check the upload path. It will be deleted.",
                ("test",), 0, "test")
    t = time.time()
    vid, _ = upload_short(yt, d / "test.mp4", meta, "Upload test, no stream.", "private")
    print("uploaded:", vid, f"in {time.time() - t:.1f}s", flush=True)
    for i in range(40):
        item = yt.videos().list(part="status,processingDetails,contentDetails", id=vid).execute()["items"][0]
        proc = item.get("processingDetails", {}).get("processingStatus")
        print(f"  {i * 15:>4}s processing={proc} upload={item['status'].get('uploadStatus')} "
              f"privacy={item['status'].get('privacyStatus')} duration={item['contentDetails'].get('duration')}", flush=True)
        if proc in ("succeeded", "failed", "terminated") or item["status"].get("uploadStatus") in ("processed", "rejected", "failed"):
            break
        time.sleep(15)
    print("thumbnail set:", set_thumbnail(yt, vid, d / "thumb.jpg"))
    item = yt.videos().list(part="snippet,status", id=vid).execute()["items"][0]
    print("final privacy:", item["status"]["privacyStatus"], "| title:", item["snippet"]["title"],
          "| thumbnails:", sorted(item["snippet"]["thumbnails"]))
    print("shorts url: https://www.youtube.com/shorts/" + vid)


if __name__ == "__main__":
    main()
