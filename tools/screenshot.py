"""Screenshot the dashboard at laptop and iPad sizes (used to verify layout).

    python tools/screenshot.py http://127.0.0.1:8787 out_dir [--wait 3]

Uses the Chromium that Playwright finds; set PW_CHROMIUM to an executable
path to override.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

SIZES = {"laptop-1440x900": (1440, 900), "ipad-1180x820": (1180, 820)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out", type=Path)
    ap.add_argument("--wait", type=float, default=3.0, help="seconds to let live data flow before capturing")
    ap.add_argument("--prefix", default="")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    exe = os.environ.get("PW_CHROMIUM") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=exe) if exe else await p.chromium.launch()
        for name, (w, h) in SIZES.items():
            page = await browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2 if "ipad" in name else 1)
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            await page.goto(args.url, wait_until="networkidle")
            await page.wait_for_timeout(args.wait * 1000)
            overflow = await page.evaluate(
                "() => ({sw: document.documentElement.scrollWidth, sh: document.documentElement.scrollHeight,"
                " cw: innerWidth, ch: innerHeight})"
            )
            clipped = await page.evaluate(
                """() => [...document.querySelectorAll('#hdr, #strip, #ftr, .panel .ph, .panel .pb, .wallet .pb > *')]
                  .filter(e => e.offsetParent && (e.scrollWidth > e.clientWidth + 1 || e.scrollHeight > e.clientHeight + 1)
                          && getComputedStyle(e).overflowY !== 'auto')
                  .map(e => (e.id || e.className || e.tagName) + ` ${e.scrollWidth}x${e.scrollHeight} > ${e.clientWidth}x${e.clientHeight}`)"""
            )
            path = args.out / f"{args.prefix}{name}.png"
            await page.screenshot(path=str(path))
            scroll = overflow["sw"] > overflow["cw"] or overflow["sh"] > overflow["ch"]
            print(f"{path}  page {overflow['sw']}x{overflow['sh']} in viewport {overflow['cw']}x{overflow['ch']}"
                  f"{'  SCROLLS!' if scroll else ''}")
            for c in clipped:
                print(f"  clipped: {c}")
            for e in errors:
                print(f"  console error: {e}")
            await page.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
