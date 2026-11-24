# Project Factory settings

These real Playwright/Chromium captures show the Factory controls in a disposable
project fixture, with factory execution disabled. Each image is a focused,
scrolled viewport: the browser opens the enclosing details and scrolls to the
actual Factory control. The images were not edited after capture and do not show
the full project page.

| Viewport | Light | Dark |
| --- | --- | --- |
| Desktop, 1440 × 1000 | [Capture](desktop-light.png) | [Capture](desktop-dark.png) |
| Mobile, 390 × 844 | [Capture](mobile-light.png) | [Capture](mobile-dark.png) |

The controls show **Use project value**, disabled JSON and a visible instruction
to use `{"enabled": false}`. [DOM QA](dom-qa.json) also verifies that Factory's
source options are exactly Inherit / Use project value; the collapsed screenshots
alone do not show the full option list.

[Manifest](manifest.json) records each PNG's original/published name, byte count,
hash, viewport/theme, capture mode and exact source/worker binding.
[Browser report](browser-report.json) is the unchanged canonical report for the
successful registered operation. [Operation](operation.json) retains selected
non-secret completion/termination fields; private state and the capture archive
are not committed. All four receipts explicitly say `focus: factory` and
`full_page: false`.

The capture used composite fixture revision `b8551191d87d252538346b0412110578afaa7198`,
including routing visual code `5594b49e1144e25cdb900e22d6ed17f205391a47` and UI code
`8e6c39465b6f4463e55d4f924b8c3bfa3e7ee0ce`. The worker's source inventory and an
independent image inspection confirm the exact form/template hashes in the
manifest match the routing branch. This asset commit changes no runtime/UI code.

Independent UX/UI review approved these four exact frames and bindings with no
material findings; a second visual inspection confirmed readable controls/help in
both sizes/themes. This is focused fixture UI evidence, not full-page, live
provider, human-merge or deployed-stack qualification.
