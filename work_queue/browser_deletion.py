"""Exercise seeded deletion fixtures in the disposable Docker preview."""

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright, expect


BASE = "http://deletion-preview:8000"
ARTIFACTS = Path("/artifacts")


async def delete_link(page, path, name, kind):
    await page.goto(BASE + path, wait_until="networkidle")
    row = page.get_by_role("row").filter(has_text=name)
    await row.get_by_role("link", name=re.compile("^Delete")).click()
    await expect(
        page.get_by_role("heading", name="Delete " + kind, exact=True)
    ).to_be_visible()


async def confirm(page, kind):
    async with page.expect_response(
        lambda response: response.request.method == "POST"
    ) as result:
        await page.get_by_role("button", name="Delete " + kind, exact=True).click()
    assert (await result.value).status == 302
    await page.wait_for_load_state("networkidle")


async def exercise(browser, name, width, height):
    context = await browser.new_context(viewport={"width": width, "height": height})
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("requestfailed", lambda request: errors.append(request.url))
    prefix = name + " deletion "

    await delete_link(page, "/schedules/", prefix + "schedule", "schedule")
    await expect(page.get_by_label("Move assigned coding runners to")).to_be_visible()
    await page.get_by_label("Move assigned coding runners to").select_option(
        label="Always on"
    )
    await page.screenshot(
        path=str(ARTIFACTS / f"ui-delete-schedule-{name}.png"), full_page=True
    )
    await confirm(page, "schedule")
    await expect(
        page.get_by_role("row").filter(has_text=prefix + "schedule")
    ).to_have_count(0)

    await page.goto(BASE + "/coding-runners/", wait_until="networkidle")
    await expect(
        page.get_by_role("row").filter(has_text=prefix + "runner")
    ).to_contain_text("Always on")
    await delete_link(page, "/coding-runners/", prefix + "runner", "coding runner")
    await confirm(page, "coding runner")
    await expect(
        page.get_by_role("row").filter(has_text=prefix + "runner")
    ).to_have_count(0)

    await delete_link(page, "/", prefix + "work", "work item")
    await page.get_by_role("link", name="Cancel", exact=True).click()
    await expect(
        page.get_by_role("row").filter(has_text=prefix + "work")
    ).to_have_count(1)
    await delete_link(page, "/", prefix + "work", "work item")
    await confirm(page, "work item")
    await expect(
        page.get_by_role("row").filter(has_text=prefix + "work")
    ).to_have_count(0)

    await delete_link(page, "/credentials/", prefix + "credential", "credential")
    await page.screenshot(
        path=str(ARTIFACTS / f"ui-delete-credential-{name}.png"), full_page=True
    )
    await confirm(page, "credential")
    await expect(
        page.get_by_role("row").filter(has_text=prefix + "credential")
    ).to_have_count(0)

    await delete_link(page, "/projects/", prefix + "project", "project")
    await page.screenshot(
        path=str(ARTIFACTS / f"ui-delete-project-{name}.png"), full_page=True
    )
    await confirm(page, "project")
    await expect(
        page.get_by_role("row").filter(has_text=prefix + "project")
    ).to_have_count(0)
    await page.goto(BASE + "/", wait_until="networkidle")
    await expect(
        page.get_by_role("row").filter(has_text=name + " project remaining work")
    ).to_have_count(0)
    overflow = await page.evaluate(
        "document.documentElement.scrollWidth > window.innerWidth + 1"
    )
    assert not overflow
    assert not errors, errors
    await context.close()
    return {
        "viewport": name,
        "width": width,
        "height": height,
        "deleted": ["schedule", "coding runner", "work item", "credential", "project"],
        "cancel_preserved_item": True,
        "page_errors": errors,
    }


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        desktop = await exercise(browser, "desktop", 1440, 1000)
        mobile = await exercise(browser, "mobile", 390, 844)
        await browser.close()
    report = {"browser": "chromium", "checks": [desktop, mobile]}
    (ARTIFACTS / "ui-deletion.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(main())
