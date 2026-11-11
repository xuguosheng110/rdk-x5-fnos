#!/usr/bin/env python3
"""Read-only reconnaissance of the fnOS first-boot Web page (headless Chrome).

Uses the locally installed Google Chrome via Playwright (channel="chrome").
It navigates, waits, and records title/text/inputs/buttons/a screenshot/HTML.
It never submits a form and never writes a credential.

  ~/.local/share/fnos-web-venv/bin/python fnos/integration/hardware/web_probe.py \
    --url http://192.168.3.x/ --out-dir /tmp/fnos-web
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out-dir", type=Path, default=Path("/tmp/fnos-web"))
    ap.add_argument("--wait-ms", type=int, default=8000)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True,
                                    args=["--ignore-certificate-errors"])
        page = browser.new_page(ignore_https_errors=True,
                                viewport={"width": 1440, "height": 900})
        responses = []

        def note(resp):
            if resp.request.resource_type in ("xhr", "fetch"):
                responses.append({"status": resp.status, "url": resp.url,
                                  "method": resp.request.method})

        page.on("response", note)
        page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(args.wait_ms)
        info = {
            "schema": "fnos-web-probe/v1",
            "url": args.url,
            "final_url": page.url,
            "title": page.title(),
            "text": re.sub(r"\n{3,}", "\n\n", page.inner_text("body"))[:6000],
            "inputs": page.eval_on_selector_all(
                "input",
                "els => els.map(e => ({type:e.type, name:e.name, id:e.id, "
                "placeholder:e.placeholder, autocomplete:e.autocomplete}))"),
            "buttons": page.eval_on_selector_all(
                "button",
                "els => els.map(e => (e.innerText||'').trim()).filter(Boolean)"),
            "links": page.eval_on_selector_all(
                "a", "els => els.map(e => ({text:(e.innerText||'').trim(), href:e.href}))")[:40],
            "xhr": responses,
            "html_path": str(args.out_dir / "page.html"),
            "screenshot": str(args.out_dir / "page.png"),
        }
        (args.out_dir / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(args.out_dir / "page.png"), full_page=True)
        (args.out_dir / "probe.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        browser.close()
    print(json.dumps({k: info[k] for k in ("final_url", "title", "inputs", "buttons")},
                     ensure_ascii=False, indent=2))
    print(f"text/HTML/screenshot -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
