"""Capture real browser PNGs from the disposable Docker application."""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path("/artifacts")
BASE = "http://vibearr:8000"


async def main():
    ROOT.mkdir(mode=0o700, exist_ok=True)
    errors, requests, images = [], [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        for name, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
            context = await browser.new_context(
                viewport={"width": width, "height": height}, color_scheme="light"
            )
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("requestfailed", lambda request: requests.append(request.url))
            response = await page.goto(BASE, wait_until="networkidle")
            if response is None or response.status != 200:
                raise RuntimeError(
                    "The Docker application did not serve its queue page."
                )
            await page.get_by_role("heading", name="Leave it for the night.").wait_for()
            style = await context.request.get(BASE + "/static/work_queue/queue.css")
            if style.status != 200 or "text/css" not in style.headers.get(
                "content-type", ""
            ):
                raise RuntimeError(
                    "Released static styles are missing from the application image."
                )
            light = ROOT / ("queue-" + name + "-light.png")
            await page.screenshot(path=str(light), full_page=True)
            await page.get_by_role("button", name="Switch to dark theme").click()
            await page.get_by_role("button", name="Switch to light theme").wait_for()
            dark = ROOT / ("queue-" + name + "-dark.png")
            await page.screenshot(path=str(dark), full_page=True)
            for path in (light, dark):
                if (
                    not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                    or path.stat().st_size < 10000
                ):
                    raise RuntimeError("The browser did not produce a substantive PNG.")
                images.append({"path": path.name, "bytes": path.stat().st_size})
            await context.close()
        await browser.close()
    if errors or requests:
        raise RuntimeError(
            "Browser errors or failed requests: "
            + json.dumps({"errors": errors, "requests": requests})
        )
    report = {
        "schema_version": 1,
        "kind": "docker-browser-smoke",
        "browser": "chromium",
        "screenshots": images,
        "page_errors": errors,
        "failed_requests": requests,
    }
    (ROOT / "browser.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
