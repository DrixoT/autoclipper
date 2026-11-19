import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from .meta import Meta
from .upload import describe


# Every locator Studio needs, in one place: Studio's DOM changes often. ("css", ...) is used only for native file inputs.
LOCATORS = {
    "create": ("button", "Create"),
    "upload_item": ("menuitem", "Upload videos"),
    "upload_file": ("css", 'input[type="file"]'),
    "title": ("textbox", "Title"),
    "description": ("textbox", "Description"),
    "not_for_kids": ("radio", "No, it's not made for kids"),
    "next": ("button", "Next"),
    "public": ("radio", "Public"),
    "unlisted": ("radio", "Unlisted"),
    "private": ("radio", "Private"),
    "publish": ("button", "Publish"),
    "save": ("button", "Save"),
    "video_link": ("link", "Video link"),
    "thumb_upload": ("css", 'input[type="file"]'),
}

STUDIO_URL = "https://studio.youtube.com"
TIMEOUT_MS = 120_000
_VIDEO_ID = re.compile(r"(?:youtu\.be/|/shorts/|/video/|[?&]v=)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")


def launch_login(profile: Path) -> None:
    subprocess.run(["open", "-na", "Google Chrome", "--args",
                    f"--user-data-dir={profile}", STUDIO_URL], check=True)


@contextmanager
def context(profile: Path, headless: bool = True):
    playwright = sync_playwright().start()
    browser_context = None
    try:
        browser_context = playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=headless)
        yield browser_context
    finally:
        if browser_context is not None:
            browser_context.close()
        playwright.stop()


def _role(page, key):
    role, name = LOCATORS[key]
    return page.locator(name).first if role == "css" else page.get_by_role(role, name=name, exact=True)


def _wait(locator, step, state="visible"):
    try:
        locator.wait_for(state=state, timeout=TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"studio step '{step}' timed out") from exc
    return locator


def _action(locator, step, action, *args):
    _wait(locator, step, "attached" if action == "set_input_files" else "visible")   # Studio hides its file inputs
    try:
        return getattr(locator, action)(*args)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"studio step '{step}' timed out") from exc


def _goto(page, url, step):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"studio step '{step}' timed out") from exc


def logged_in(ctx, *, base: str = STUDIO_URL) -> bool:
    page = ctx.new_page()
    _goto(page, base, "open Studio")
    return urlparse(page.url).hostname != "accounts.google.com"


def video_id_from_url(url: str) -> str | None:
    match = _VIDEO_ID.search(url)
    return match.group(1) if match else None


def studio_upload(ctx, video: Path, meta: Meta, credit: str, privacy: str,
                  *, base: str = STUDIO_URL, channel_id: str | None = None, short: bool = True) -> str:
    page = ctx.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    if channel_id:
        channel_path = f"/channel/{channel_id}"
        _goto(page, f"{base}{channel_path}", "open Studio channel")
        create = _wait(_role(page, "create"), "create")
        current = urlparse(page.url)
        if current.netloc != urlparse(base).netloc or current.path.rstrip("/") != channel_path:
            raise RuntimeError("YouTube Studio did not open the authorized channel")
        _action(create, "create", "click")
    else:
        _goto(page, base, "open Studio")
        _action(_role(page, "create"), "create", "click")
    _action(_role(page, "upload_item"), "upload videos", "click")
    _action(_role(page, "upload_file"), "select video", "set_input_files", str(video))
    _action(_role(page, "title"), "title", "fill", meta.title)
    _action(_role(page, "description"), "description", "fill", describe(meta, credit, short))
    _action(_role(page, "not_for_kids"), "made for kids", "check")

    privacy_key = privacy.lower()
    if privacy_key not in ("public", "unlisted", "private"):
        raise ValueError(f"unknown privacy: {privacy}")
    for _ in range(4):
        if _role(page, privacy_key).count():
            break
        _action(_role(page, "next"), "next", "click")
    selected = _wait(_role(page, privacy_key), "visibility")
    _action(selected, "visibility", "check")
    action = _role(page, "publish")
    if not action.count():
        action = _role(page, "save")
    _action(action, "publish", "click")
    try:
        page.get_by_role("dialog").wait_for(state="visible", timeout=TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("studio step 'publish' timed out") from exc
    for link in page.get_by_role("link").all():
        href = link.get_attribute("href") or ""
        if "youtu.be" in href or "/shorts/" in href:
            video_id = video_id_from_url(href)
            if video_id:
                return video_id
    raise RuntimeError("Studio did not provide a video link")


def studio_set_thumbnail(ctx, video_id: str, jpg: Path, *, base: str = STUDIO_URL) -> None:
    page = ctx.new_page()
    _goto(page, f"{base}/video/{video_id}/edit", "open video edit")
    _action(_role(page, "thumb_upload"), "thumbnail", "set_input_files", str(jpg))
    _action(_role(page, "save"), "save thumbnail", "click")
