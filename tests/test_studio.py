import re
from pathlib import Path

import pytest

from autoclipper.meta import Meta
from autoclipper import studio
from autoclipper.studio import context, launch_login, studio_upload, video_id_from_url


@pytest.mark.parametrize("url,want", [("https://youtube.com/shorts/aB3_-xYz012", "aB3_-xYz012"),
    ("https://youtu.be/aB3_-xYz012", "aB3_-xYz012"), ("https://studio.youtube.com/video/aB3_-xYz012/edit", "aB3_-xYz012"),
    ("https://youtube.com/", None)])
def test_video_id_from_url(url, want):
    assert video_id_from_url(url) == want


def test_launch_login_argv(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(studio.subprocess, "run", lambda argv, check: seen.append((argv, check)))
    launch_login(tmp_path / "profile")
    assert seen == [(["open", "-na", "Google Chrome", "--args",
                      f"--user-data-dir={tmp_path / 'profile'}", "https://studio.youtube.com"], True)]


@pytest.mark.skipif(not Path("/Applications/Google Chrome.app").exists(), reason="no Chrome")
def test_studio_upload_local_fixture(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "studio" / "upload.html"
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    meta = Meta("Chat had a wild moment", "WILD MOMENT", "A clean description for this clip.",
                ("stream",), 0, "template")
    with context(tmp_path / "profile", headless=True) as ctx:
        video_id = studio_upload(ctx, video, meta, "Clipped from Stream's live stream: https://example.test",
                                 "unlisted", base=fixture.as_uri())
        page = ctx.pages[-1]
        result = page.locator("#result").inner_text()
    assert video_id == "aB3_-xYz012"
    assert "Chat had a wild moment" in result
    # contenteditable innerText renders the blank line twice, so check the parts in order
    assert re.search(r"A clean description for this clip\.(\\n)+Clipped from Stream's live stream: https://example\.test\\n#shorts", result)
    assert "unlisted" in result


@pytest.mark.skipif(not Path("/Applications/Google Chrome.app").exists(), reason="no Chrome")
def test_studio_upload_publish_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "TIMEOUT_MS", 1500)
    fixture = Path(__file__).parent / "fixtures" / "studio" / "timeout.html"
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    meta = Meta("Chat had a wild moment", "WILD MOMENT", "A clean description for this clip.",
                ("stream",), 0, "template")
    with context(tmp_path / "profile", headless=True) as ctx:
        with pytest.raises(RuntimeError, match="studio step 'publish' timed out"):
            studio_upload(ctx, video, meta, "credit", "private", base=fixture.as_uri())


def test_studio_refuses_to_upload_to_a_different_channel(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    ctx = MagicMock()
    page = ctx.new_page.return_value
    page.url = "https://studio.youtube.com/channel/wrong"
    meta = Meta("Title", "Hook", "Description", (), 0, "template")

    with pytest.raises(RuntimeError, match="did not open the authorized channel"):
        studio_upload(ctx, tmp_path / "video.mp4", meta, "credit", "private", channel_id="expected")

    page.goto.assert_called_once_with(
        "https://studio.youtube.com/channel/expected", wait_until="domcontentloaded", timeout=studio.TIMEOUT_MS
    )
    page.get_by_role.return_value.click.assert_not_called()
