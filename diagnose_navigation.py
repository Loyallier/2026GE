#!/usr/bin/env python3
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://ac.xmu.edu.my"
PROFILE_DIR = Path(__file__).resolve().parent / ".collector_diag_profile"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def interesting(url: str) -> bool:
    u = (url or "").lower()
    return "ac.xmu.edu.my" in u and (
        "c=xk" in u
        or "/student/" in u
        or "index.php" in u
    )


def dump_state(context) -> None:
    print(f"\n[{ts()}] ===== STATE DUMP =====")
    pages = [p for p in context.pages if not p.is_closed()]
    print(f"pages={len(pages)}")

    for pi, page in enumerate(pages, 1):
        try:
            print(f"  TAB {pi}: {page.url}")
            for fi, frame in enumerate(page.frames, 1):
                print(f"    FRAME {fi}: {frame.url}")

            iframe_info = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('iframe, frame')).map((el, i) => ({
                    i,
                    tag: el.tagName,
                    id: el.id || '',
                    name: el.getAttribute('name') || '',
                    src: el.getAttribute('src') || '',
                    outer: el.outerHTML.slice(0, 500)
                }))
                """
            )
            for item in iframe_info:
                print(
                    "    ELEMENT "
                    f"{item['tag']}#{item['id']} "
                    f"name={item['name']!r} src={item['src']!r}"
                )

            links = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({text:(a.innerText||'').trim(), href:a.href}))
                    .filter(x => /c=Xk/i.test(x.href) || /\/student\//i.test(x.href))
                    .slice(0, 30)
                """
            )
            for item in links:
                print(f"    LINK: {item['text']!r} -> {item['href']}")

            forms = page.evaluate(
                """
                () => Array.from(document.forms).map((f, i) => ({
                    i,
                    action: f.action || '',
                    method: f.method || '',
                    target: f.target || ''
                }))
                """
            )
            for item in forms:
                if interesting(item["action"]):
                    print(
                        f"    FORM {item['i']}: method={item['method']} "
                        f"target={item['target']!r} action={item['action']}"
                    )

        except Exception as exc:
            print(f"  TAB {pi}: state read failed: {exc}")

    print(f"[{ts()}] ======================\n")


def main() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            no_viewport=True,
            args=["--start-maximized"],
        )

        def on_page(page):
            print(f"[{ts()}] NEW TAB: {page.url}")
            page.on("framenavigated", lambda frame: print(
                f"[{ts()}] FRAME NAV: {frame.url}"
            ))

        for page in context.pages:
            on_page(page)
        context.on("page", on_page)

        def on_request(req):
            if interesting(req.url):
                print(f"[{ts()}] REQ  {req.method:<4} {req.url}")

        def on_response(resp):
            if interesting(resp.url):
                print(f"[{ts()}] RESP {resp.status:<3} {resp.url}")

        context.on("request", on_request)
        context.on("response", on_response)

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"[{ts()}] initial navigation warning: {exc}")

        print(
            "\n请在这个 Chromium 里：\n"
            "1) 登录；\n"
            "2) 正常点击进入你刚才的选课/查看页面；\n"
            "3) 到达你认为目标页面后，回到 PowerShell 按 Enter。\n"
            "期间所有相关网络请求会实时打印。\n"
        )

        try:
            input("到达目标页面后按 Enter 做最终状态 dump > ")
        except KeyboardInterrupt:
            pass

        dump_state(context)
        input("复制上面的输出后，按 Enter 关闭诊断浏览器 > ")
        context.close()


if __name__ == "__main__":
    main()
