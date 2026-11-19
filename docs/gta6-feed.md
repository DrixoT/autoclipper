# GTA VI feed

An editorial guide for a clip feed about one game, Grand Theft Auto VI (the game in `streams.example.toml`), and the
setup for posting its Shorts as Instagram Reels.

Publish a useful, verifiable detail as soon as the source supports it. Include enough context for a new viewer to understand the clip, and show the event and the streamer's reaction together.

| Format | What to publish | Evidence to keep |
| --- | --- | --- |
| Official news | Release and preload details, patch notes, trailers and Rockstar announcements | The specific primary-source announcement, its date, and the platform or region it applies to |
| Tips and tutorials | One repeatable task with the setup, steps and visible result | A recording that shows the steps, plus the game version, platform and relevant prerequisites |
| Gameplay clips | A 1–5 minute story on YouTube and a 30–180 second Short and Reel from it | The creator's name, original stream or VOD URL, timestamp and clipping or reuse permission |
| GTA V nostalgia | Recognizable missions, places, mechanics or moments from GTA V | The original footage source and a clear GTA V label in the video and caption |
| Launch coverage | A confirmed launch milestone, first impressions or a verified player experience | The official milestone or the original, credited gameplay plus the platform and region |

Start with [Rockstar's GTA VI Newswire](https://www.rockstargames.com/newswire?tag=722) and the [official GTA VI page](https://www.rockstargames.com/VI). Use the relevant platform's official notice to confirm any platform-specific claims. For corrections, update the affected post and state what changed. Use chat to locate reactions. Publish gameplay credit with enough detail for a viewer to find the original. Hold leaks, rumors and unverified claims out of the feed.

Track time from the verified source to publication, posts with a visible source, and corrections. The editorial goal is fast publication with all three story beats and an accurate, specific title.

## Instagram

1. Create a professional Instagram account (Creator or Business) and link it to a Facebook Page. Use a Meta app with Facebook Login for Business for autoclipper's direct local-file upload.
2. Obtain a Facebook Page access token for that account with `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`, `pages_show_list` and `business_management` (the last two are on the Meta app's own required list). Make it a Page token derived from a long-lived user token, so it does not expire; its data access still lapses after about three months, so authorize again before then. Record the Instagram professional account ID.
3. Set `instagram_account_id` in the run config. Run `uv run autoclipper --config <config> instagram-auth <token-file>` with the token in a local file. autoclipper verifies the configured account ID and stores the token in macOS Keychain.
4. With `upload = true` and `privacy = "public"`, the worker queues a Reel for each public YouTube Short and waits for Meta to finish processing it. The Reel uses the same vertical video, a cover frame from the moment, and creator credit in the caption.

Meta records Reel publishing separately from YouTube's upload limit. A Reel whose publication is uncertain is held for inspection to avoid a duplicate. Check the Instagram account before retrying such a Reel manually. [Meta content publishing documentation](https://developers.facebook.com/documentation/instagram-platform/content-publishing)
