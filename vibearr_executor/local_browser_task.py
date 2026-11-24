"""Image-owned Chromium capture, separate from the candidate app identity."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from vibearr_runner.browser_receipts import (
    VIEWPORTS,
    capture_worker_receipt,
    validate_binding,
    validate_capture_mode,
    validate_page_path,
    validate_worker_binding,
)

BASE = "http://127.0.0.1:8765"
FACTORY_ANCESTORS = """element => {
    for (let node = element.parentElement; node; node = node.parentElement) {
        if (node.tagName === 'DETAILS') node.open = true;
    }
}"""


def record_error(target, message):
    if len(target) < 100:
        target.append(str(message)[:1000])


def boundary_checks():
    denied = False
    try:
        list(Path("/control").iterdir())
    except PermissionError:
        denied = True
    changed = False
    try:
        (Path("/work") / ".browser-write-probe").write_text("unexpected")
        changed = True
    except PermissionError:
        pass
    for path in Path("/work").rglob("*"):
        if not path.is_file():
            continue
        try:
            path.chmod(0o644)
            changed = True
        except PermissionError:
            pass
        break
    checks = {
        "control_paths_denied": denied,
        "credentials_scoped": not Path("/secrets/backend").exists(),
        "source_write_denied": not changed,
    }
    if not all(checks.values()):
        raise RuntimeError("The browser phase lacks its registered read-only boundary.")
    return checks


async def capture():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/playwright"
    from playwright.async_api import async_playwright

    value = json.loads(Path("/input/browser.json").read_bytes())
    binding = validate_binding(value["source_binding"])
    worker = validate_worker_binding(value["worker_binding"])
    page_path = validate_page_path(value["page_path"])
    focus = value.get("capture_focus", "page")
    mode = validate_capture_mode({"focus": focus, "full_page": focus == "page"})
    if focus == "factory" and not page_path.startswith("/projects/"):
        raise RuntimeError("Factory focus requires a registered local project page.")
    checks = boundary_checks()
    root = Path.home() / "browser"
    capture_root = root / ("capture-" + uuid4().hex)
    capture_root.mkdir(mode=0o700)
    errors, failed, frames = [], [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        for name, viewport in VIEWPORTS.items():
            context = await browser.new_context(viewport=viewport, color_scheme="light")

            async def route(request):
                if request.request.url.startswith(BASE + "/"):
                    await request.continue_()
                else:
                    record_error(failed, "Disallowed browser destination.")
                    await request.abort()

            await context.route("**/*", route)
            page = await context.new_page()
            page.on("pageerror", lambda error: record_error(errors, error))
            page.on("requestfailed", lambda request: record_error(failed, request.url))

            def console_error(message):
                if message.type == "error":
                    record_error(errors, message.text)

            def http_error(response):
                if response.status >= 400:
                    record_error(failed, f"HTTP {response.status}: {response.url}")

            page.on("console", console_error)
            page.on("response", http_error)
            for theme in ("light", "dark"):
                await page.emulate_media(color_scheme=theme)
                target = BASE + page_path + "?theme=" + theme
                response = await page.goto(
                    target, wait_until="networkidle", timeout=30000
                )
                if response is None or response.status != 200 or page.url != target:
                    raise RuntimeError(
                        "The candidate UI did not serve the bound local page."
                    )
                heading = page.locator("main h1").first
                await heading.wait_for(timeout=10000)
                if not (await heading.inner_text()).strip():
                    raise RuntimeError(
                        "The candidate page has no visible main heading."
                    )
                details = page.locator(
                    'section[aria-label="Factory rounds and evidence"] > details'
                ).first
                if not await details.count():
                    details = page.locator("main details").first
                if await details.count():
                    await details.evaluate("element => { element.open = true; }")
                if focus == "factory":
                    field = page.locator('select[name="runner-factory_mode"]')
                    await field.evaluate(FACTORY_ANCESTORS)
                    await field.scroll_into_view_if_needed()
                    await page.evaluate("window.scrollBy(0, -100)")
                style = await context.request.get(
                    BASE + "/static/work_queue/queue.css", max_redirects=0
                )
                if style.status != 200 or "text/css" not in style.headers.get(
                    "content-type", ""
                ):
                    raise RuntimeError(
                        "The candidate application has no released queue styling."
                    )
                path = capture_root / f"queue-{name}-{theme}.png"
                await page.screenshot(path=str(path), full_page=mode["full_page"])
                frames.append(
                    capture_worker_receipt(
                        binding,
                        worker,
                        path,
                        receipt_path=Path("/screenshots")
                        / capture_root.name
                        / path.name,
                        url=page.url,
                        viewport=viewport,
                        theme=theme,
                        focus=focus,
                        full_page=mode["full_page"],
                    )
                )
            await context.close()
        await browser.close()
    if errors or failed:
        raise RuntimeError(
            "Candidate browser page errors or failed requests prevent capture acceptance."
        )
    report = {
        "schema_version": 3,
        "kind": "compact-worker-browser-acceptance",
        "browser": "chromium",
        "source_binding": binding,
        "worker_binding": worker,
        "screenshots": frames,
        "page_errors": errors,
        "failed_requests": failed,
        "boundary_checks": checks,
    }
    (capture_root / "browser.json").write_text(
        json.dumps(report, sort_keys=True) + "\n"
    )


def main():
    os.umask(0o077)
    asyncio.run(capture())


if __name__ == "__main__":
    main()
