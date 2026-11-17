"""Capture real browser PNGs from the disposable Docker application."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from playwright.async_api import async_playwright
from vibearr_runner.browser_receipts import capture_receipt, png_bytes, validate_binding


ROOT = Path("/artifacts")
BASE = "http://vibearr:8000"


def image_record(path, *, binding, url, viewport, theme):
    if binding is None:
        return {"path": path.name, "bytes": len(png_bytes(path))}
    return capture_receipt(
        binding,
        path,
        receipt_path=Path("/screenshots") / path.relative_to(ROOT),
        url=url,
        viewport=viewport,
        theme=theme,
    )


async def main():
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, exist_ok=True)
    raw_binding = os.environ.get("VIBEARR_BROWSER_SOURCE_BINDING", "")
    if len(raw_binding) > 8192:
        raise ValueError("Browser source binding exceeds its byte limit.")
    binding = validate_binding(json.loads(raw_binding)) if raw_binding else None
    capture = ROOT
    if binding is not None:
        capture = ROOT / ("capture-" + uuid4().hex)
        capture.mkdir(mode=0o700)
    errors, requests, images = [], [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        for name, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
            viewport = {"width": width, "height": height}
            context = await browser.new_context(viewport=viewport, color_scheme="light")
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("requestfailed", lambda request: requests.append(request.url))
            response = await page.goto(BASE + "/?theme=light", wait_until="networkidle")
            if response is None or response.status != 200:
                raise RuntimeError(
                    "The Docker application did not serve its queue page."
                )
            heading = page.locator("main h1").first
            await heading.wait_for()
            if not (await heading.inner_text()).strip():
                raise RuntimeError("The queue page is missing its main heading.")
            style = await context.request.get(BASE + "/static/work_queue/queue.css")
            if style.status != 200 or "text/css" not in style.headers.get(
                "content-type", ""
            ):
                raise RuntimeError(
                    "Released static styles are missing from the application image."
                )
            light = capture / ("queue-" + name + "-light.png")
            await page.screenshot(path=str(light), full_page=True)
            images.append(
                image_record(
                    light,
                    binding=binding,
                    url=page.url,
                    viewport=viewport,
                    theme="light",
                )
            )
            await page.get_by_role("button", name="Switch to dark theme").click()
            await page.get_by_role("button", name="Switch to light theme").wait_for()
            dark = capture / ("queue-" + name + "-dark.png")
            await page.screenshot(path=str(dark), full_page=True)
            images.append(
                image_record(
                    dark, binding=binding, url=page.url, viewport=viewport, theme="dark"
                )
            )
            await context.close()
        await browser.close()
    if errors or requests:
        raise RuntimeError(
            "Browser errors or failed requests: "
            + json.dumps({"errors": errors, "requests": requests})
        )
    report = {
        "schema_version": 2 if binding else 1,
        "kind": "docker-browser-acceptance" if binding else "docker-browser-smoke",
        "browser": "chromium",
        "screenshots": images,
        "page_errors": errors,
        "failed_requests": requests,
    }
    if binding:
        report["source_binding"] = binding
        report["report_path"] = str(
            Path("/screenshots") / capture.relative_to(ROOT) / "browser.json"
        )
    (capture / "browser.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
