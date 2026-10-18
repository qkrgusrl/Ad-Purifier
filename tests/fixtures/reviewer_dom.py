"""reviewer 반례용 실제 DOM 캡처. HTML은 로컬 문자열이며 외부 요청은 차단한다."""
import os
from pathlib import Path
from unittest.mock import patch

from fixtures.ai_contract import ROOT, URL


def capture(html):
    import playwright
    from playwright.sync_api import sync_playwright

    local = Path(playwright.__file__).parent / "driver/package/.local-browsers"
    env = {"PLAYWRIGHT_BROWSERS_PATH": "0"} if local.is_dir() else {}
    with patch.dict(os.environ, env), sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers="block")
            context.route("**/*", lambda route: route.abort())
            context.route_web_socket("**/*", lambda ws: ws.close())
            page = context.new_page()
            page.set_content('<!doctype html><meta charset="utf-8">' + html, wait_until="load")
            data = page.evaluate((ROOT / "detector/extract.js").read_text(encoding="utf-8"))
            data["frame_url"] = URL
            return [(URL, [("", data)])]
        finally:
            browser.close()
