#!/usr/bin/env python3
"""
XMUM Round 1 observer.

Goal: collect everything first, analyze later.

Flow:
1. Launch a visible Chromium window.
2. User logs in manually and opens the Round 1 Xk page.
3. The collector auto-detects the Xk page.
4. Repeatedly reloads the same page and stores:
   - full DOM HTML (gzip-compressed in SQLite)
   - every HTML table as generic row/cell text
   - timing / URL / HTTP metadata
5. No enrollment actions are implemented.

Usage:
    pip install playwright
    playwright install chromium
    python round1_collector.py
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

BASE_URL = "https://ac.xmu.edu.my"
LOGIN_URL = f"{BASE_URL}/index.php"
DEFAULT_INTERVAL = 1.0
DEFAULT_SETTLE = 0.20
DEFAULT_TIMEOUT_MS = 30_000

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "collector_data"
PROFILE_DIR = PROJECT_DIR / ".collector_profile"


@dataclass
class CaptureResult:
    snapshot_id: int
    captured_at: str
    table_count: int
    row_count: int
    html_sha256: str


class BrowserClosed(RuntimeError):
    """The user closed the Chromium window/context."""


class SnapshotStore:
    """Append-only-ish snapshot store with durable per-snapshot commits."""

    def __init__(self, db_path: Path, jsonl_path: Path):
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self.jsonl = open(jsonl_path, "a", encoding="utf-8", buffering=1)

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS html_blobs (
                sha256 TEXT PRIMARY KEY,
                html_gzip BLOB NOT NULL,
                raw_bytes INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                local_epoch REAL NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                http_status INTEGER,
                server_date TEXT,
                load_ms REAL,
                html_sha256 TEXT NOT NULL,
                table_count INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                tables_json TEXT NOT NULL,
                error TEXT,
                FOREIGN KEY(html_sha256) REFERENCES html_blobs(sha256)
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_time
            ON snapshots(captured_at);
            """
        )
        self.conn.commit()

    def save(
        self,
        *,
        captured_at: str,
        local_epoch: float,
        url: str,
        title: str,
        http_status: int | None,
        server_date: str | None,
        load_ms: float | None,
        html: str,
        tables: list[dict[str, Any]],
        error: str | None = None,
    ) -> CaptureResult:
        raw = html.encode("utf-8", errors="replace")
        sha = hashlib.sha256(raw).hexdigest()
        compressed = gzip.compress(raw, compresslevel=6)
        row_count = sum(len(t.get("rows", [])) for t in tables)
        tables_json = json.dumps(tables, ensure_ascii=False, separators=(",", ":"))

        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO html_blobs(sha256, html_gzip, raw_bytes)
                VALUES (?, ?, ?)
                """,
                (sha, compressed, len(raw)),
            )
            cur = self.conn.execute(
                """
                INSERT INTO snapshots(
                    captured_at, local_epoch, url, title,
                    http_status, server_date, load_ms,
                    html_sha256, table_count, row_count,
                    tables_json, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at,
                    local_epoch,
                    url,
                    title,
                    http_status,
                    server_date,
                    load_ms,
                    sha,
                    len(tables),
                    row_count,
                    tables_json,
                    error,
                ),
            )
            snapshot_id = int(cur.lastrowid)

        line = {
            "snapshot_id": snapshot_id,
            "captured_at": captured_at,
            "local_epoch": local_epoch,
            "url": url,
            "title": title,
            "http_status": http_status,
            "server_date": server_date,
            "load_ms": load_ms,
            "html_sha256": sha,
            "table_count": len(tables),
            "row_count": row_count,
            "error": error,
        }
        self.jsonl.write(json.dumps(line, ensure_ascii=False) + "\n")
        self.jsonl.flush()
        os.fsync(self.jsonl.fileno())

        return CaptureResult(
            snapshot_id=snapshot_id,
            captured_at=captured_at,
            table_count=len(tables),
            row_count=row_count,
            html_sha256=sha,
        )

    def close(self) -> None:
        try:
            self.jsonl.close()
        finally:
            self.conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def is_xk_page(page: Page) -> bool:
    url = page.url.lower()
    return "ac.xmu.edu.my" in url and "c=xk" in url


def is_login_page(page: Page) -> bool:
    url = page.url.lower()
    if "c=login" in url:
        return True
    try:
        return (
            page.locator('input[name="username"]').count() > 0
            and page.locator('input[name="password"]').count() > 0
        )
    except Exception:
        return False


def extract_all_tables(page: Page) -> list[dict[str, Any]]:
    """
    Generic table dump.

    Intentionally does not depend on table index, table id, header names,
    or a particular course schema. If XMUM changes the layout, raw HTML is
    still preserved and this extractor simply records whatever tables exist.
    """
    return page.evaluate(
        """
        () => Array.from(document.querySelectorAll("table")).map((table, ti) => ({
            table_index: ti,
            id: table.id || "",
            class_name: table.className || "",
            text: (table.innerText || "").trim(),
            rows: Array.from(table.querySelectorAll("tr")).map((tr, ri) => ({
                row_index: ri,
                cells: Array.from(tr.querySelectorAll("th,td")).map((cell, ci) => ({
                    cell_index: ci,
                    tag: cell.tagName,
                    text: (cell.innerText || "").trim()
                }))
            }))
        }))
        """
    )


def _looks_like_xk(url: str) -> bool:
    u = (url or "").lower()
    return "ac.xmu.edu.my" in u and "c=xk" in u


def wait_for_enter_or_browser_close(context: BrowserContext, prompt: str) -> None:
    """Wait for Enter without becoming blind to the browser being closed."""
    done = threading.Event()

    def _reader() -> None:
        try:
            input(prompt)
        finally:
            done.set()

    threading.Thread(target=_reader, daemon=True).start()

    while not done.is_set():
        pages = [p for p in context.pages if not p.is_closed()]
        if not pages:
            raise BrowserClosed("All Chromium pages were closed.")
        time.sleep(0.2)


def choose_target_after_manual_navigation(
    context: BrowserContext,
    recent_urls: list[str],
    timeout_ms: int,
) -> Page:
    """
    User decides when the desired screen is ready.

    We deliberately do not depend on top-level URL/frame structure. After the
    user presses Enter, prefer the most recent observed c=Xk network URL and
    elevate it to a normal top-level page so subsequent reloads are reliable.
    """
    pages = [p for p in context.pages if not p.is_closed()]
    if not pages:
        raise BrowserClosed("All Chromium pages were closed.")

    page = pages[-1]

    print(
        "\n请在这个 Chromium 里正常登录，然后进入你想持续观察的页面。\n"
        "看到目标课程页面后，回到 PowerShell 按 Enter。\n"
        "程序会根据你刚才真实产生的网络请求自动找 c=Xk URL。\n"
    )

    wait_for_enter_or_browser_close(context, "目标页面已就绪后按 Enter > ")

    pages = [p for p in context.pages if not p.is_closed()]
    if not pages:
        raise BrowserClosed("All Chromium pages were closed.")
    page = pages[-1]

    candidates: list[str] = []
    seen = set()

    # Prefer what the browser currently exposes.
    for p in reversed(pages):
        try:
            if _looks_like_xk(p.url) and p.url not in seen:
                candidates.append(p.url)
                seen.add(p.url)
        except Exception:
            pass
        try:
            for frame in reversed(p.frames):
                if _looks_like_xk(frame.url) and frame.url not in seen:
                    candidates.append(frame.url)
                    seen.add(frame.url)
        except Exception:
            pass

    # Then prefer the most recent network-observed Xk URL.
    for url in reversed(recent_urls):
        if _looks_like_xk(url) and url not in seen:
            candidates.append(url)
            seen.add(url)

    if candidates:
        target = candidates[0]
        print(f"\n发现真实选课 URL: {target}")
        print("将它提升为独立顶层页面，之后只刷新这个 URL。")
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)
        page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
        print(f"已锁定采集页面: {page.url}")
        return page

    print(
        "\n没有自动发现 c=Xk 网络 URL。"
        "如果你知道真实 URL，可以直接粘贴；否则留空进入当前页面保底模式。"
    )
    manual_url = input("目标 URL（可留空）> ").strip()
    if manual_url:
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)
        page.goto(manual_url, wait_until="domcontentloaded", timeout=timeout_ms)
        print(f"已锁定采集页面: {page.url}")
        return page

    print(
        "⚠️ 未获得可直接刷新的真实 URL。将保存当前 DOM，"
        "但不会自动 browser reload，避免把你从动态页面刷回主页。"
    )
    return page

def capture_current_page(
    page: Page,
    store: SnapshotStore,
    response: Response | None,
    load_ms: float | None,
    error: str | None = None,
) -> CaptureResult:
    html = page.content()
    title = page.title()

    tables: list[dict[str, Any]] = []
    table_error: str | None = None
    try:
        tables = extract_all_tables(page)
    except Exception as exc:
        table_error = f"table extraction failed: {exc}"

    combined_error = error
    if table_error:
        combined_error = f"{error}; {table_error}" if error else table_error

    status = response.status if response is not None else None
    server_date = None
    if response is not None:
        try:
            server_date = response.headers.get("date")
        except Exception:
            pass

    return store.save(
        captured_at=now_iso(),
        local_epoch=time.time(),
        url=page.url,
        title=title,
        http_status=status,
        server_date=server_date,
        load_ms=load_ms,
        html=html,
        tables=tables,
        error=combined_error,
    )


def save_error_screenshot(page: Page, session_dir: Path, label: str) -> None:
    try:
        error_dir = session_dir / "errors"
        error_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        page.screenshot(path=str(error_dir / f"{ts}_{label}.png"), full_page=True)
    except Exception:
        pass


def build_paths() -> tuple[Path, Path, Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session_name = datetime.now().strftime("round1_%Y%m%d_%H%M%S")
    session_dir = DATA_DIR / session_name
    session_dir.mkdir(parents=True, exist_ok=True)
    return (
        session_dir,
        session_dir / "snapshots.sqlite3",
        session_dir / "timeline.jsonl",
    )


def _is_closed_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "target page, context or browser has been closed" in msg
        or "page closed" in msg
        or "browser has been closed" in msg
        or "context has been closed" in msg
    )


def wait_until_logged_in(page: Page) -> None:
    """Wait until the visible login form disappears; no Enter key required."""
    last_msg = 0.0
    while True:
        if page.is_closed():
            raise BrowserClosed("Chromium window was closed.")
        try:
            if not is_login_page(page):
                return
        except Exception as exc:
            if _is_closed_error(exc):
                raise BrowserClosed("Chromium window was closed.") from exc

        now = time.monotonic()
        if now >= last_msg:
            print("[等待登录] 请在 Chromium 中完成登录...")
            last_msg = now + 5.0
        time.sleep(0.5)


def run(interval: float, settle: float, timeout_ms: int, target_url: str | None) -> None:
    session_dir, db_path, jsonl_path = build_paths()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"采集数据目录: {session_dir}")
    print("原则：先保存完整 DOM，再尝试解析所有 table；解析失败也不会丢原始页面。")
    print("停止方式：Ctrl+C\n")

    store = SnapshotStore(db_path, jsonl_path)

    try:
        with sync_playwright() as p:
            context: BrowserContext = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                no_viewport=True,
                args=["--start-maximized"],
            )

            pages = context.pages
            page = pages[0] if pages else context.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)

            if target_url:
                print(f"目标 URL: {target_url}")
                try:
                    page.goto(LOGIN_URL, wait_until="domcontentloaded")
                except PlaywrightTimeoutError:
                    print("登录页加载超时，但浏览器已打开；你仍可手动刷新。")

                wait_until_logged_in(page)
                print("检测到登录完成，进入目标页面...")
                page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                print(f"已锁定采集页面: {page.url}")
                direct_reload = True
            else:
                recent_urls: list[str] = []
                seen_xk_urls: set[str] = set()

                def remember_url(url: str) -> None:
                    if not url:
                        return
                    recent_urls.append(url)
                    if len(recent_urls) > 500:
                        del recent_urls[:-500]
                    if _looks_like_xk(url) and url not in seen_xk_urls:
                        seen_xk_urls.add(url)
                        print(f"[发现 Xk 请求] {url}")

                context.on("request", lambda req: remember_url(req.url))
                context.on("response", lambda resp: remember_url(resp.url))

                try:
                    page.goto(LOGIN_URL, wait_until="domcontentloaded")
                except PlaywrightTimeoutError:
                    print("登录页加载超时，但浏览器已打开；你仍可手动刷新/登录。")

                page = choose_target_after_manual_navigation(context, recent_urls, timeout_ms)
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)
                direct_reload = _looks_like_xk(page.url)

            print("\n开始采集。程序只观察页面，不执行选课/退课动作。")
            print("窗口现在可自由缩放/最大化；页面缩放可直接用 Ctrl+- / Ctrl++ / Ctrl+0。\n")

            previous_row_count: int | None = None
            snapshot_count = 0

            try:
                result = capture_current_page(page, store, None, None)
                snapshot_count += 1
                previous_row_count = result.row_count
                print(
                    f"[{result.captured_at}] #{result.snapshot_id} 初始页 "
                    f"tables={result.table_count} rows={result.row_count}"
                )
            except Exception as exc:
                print(f"初始页面保存失败: {exc}")
                save_error_screenshot(page, session_dir, "initial_capture_failed")

            while True:
                if page.is_closed():
                    raise BrowserClosed("Chromium window was closed.")

                time.sleep(max(0.0, interval))

                if page.is_closed():
                    raise BrowserClosed("Chromium window was closed.")

                started = time.perf_counter()
                response: Response | None = None
                nav_error: str | None = None

                if direct_reload:
                    try:
                        response = page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                    except PlaywrightTimeoutError as exc:
                        nav_error = f"reload timeout: {exc}"
                    except Exception as exc:
                        if _is_closed_error(exc):
                            raise BrowserClosed("Chromium window was closed.") from exc
                        nav_error = f"reload failed: {exc}"
                else:
                    # Dynamic-shell fallback: never reload index.php automatically,
                    # because doing so may destroy the manually selected view.
                    nav_error = "dynamic-shell fallback: DOM snapshot only, no reload"

                load_ms = (time.perf_counter() - started) * 1000.0

                if settle > 0:
                    time.sleep(settle)

                try:
                    result = capture_current_page(
                        page, store, response, load_ms, error=nav_error
                    )
                    snapshot_count += 1

                    warning = ""
                    if result.table_count == 0 or result.row_count == 0:
                        warning = "  ⚠️ 页面没有表格/行，但原始 HTML 已保存"
                        save_error_screenshot(page, session_dir, "empty_tables")
                    elif (
                        previous_row_count
                        and result.row_count < max(1, previous_row_count // 2)
                    ):
                        warning = (
                            f"  ⚠️ 行数骤降 {previous_row_count}->{result.row_count}，"
                            "仍已完整保存"
                        )
                        save_error_screenshot(page, session_dir, "row_drop")

                    previous_row_count = result.row_count or previous_row_count

                    status = response.status if response is not None else "?"
                    print(
                        f"[{result.captured_at}] #{result.snapshot_id} "
                        f"HTTP={status} load={load_ms:.0f}ms "
                        f"tables={result.table_count} rows={result.row_count}"
                        f"{warning}"
                    )
                except Exception as exc:
                    if _is_closed_error(exc) or page.is_closed():
                        raise BrowserClosed("Chromium window was closed.") from exc
                    print(f"本轮保存失败: {exc}")
                    save_error_screenshot(page, session_dir, "capture_failed")


            context.close()

    except KeyboardInterrupt:
        print("\nCtrl+C：立即停止采集。")
    except BrowserClosed:
        print("\n检测到 Chromium 已关闭，自动停止采集。")
    finally:
        store.close()
        print(f"SQLite: {db_path}")
        print(f"时间线:  {jsonl_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="XMUM 第一轮只读全量页面采集器（手动登录，自动刷新，全量保存）"
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="直接指定要持续刷新的 c=Xk 页面 URL；推荐正式采集时使用",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"每轮加载完成后的等待秒数（默认 {DEFAULT_INTERVAL}s）",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE,
        help=f"页面加载后等待 DOM 稳定的秒数（默认 {DEFAULT_SETTLE}s）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_MS / 1000,
        help="单次页面加载超时秒数（默认 30s）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.interval < 0 or args.settle < 0 or args.timeout <= 0:
        print("interval/settle 必须 >= 0，timeout 必须 > 0", file=sys.stderr)
        raise SystemExit(2)

    run(
        interval=args.interval,
        settle=args.settle,
        timeout_ms=int(args.timeout * 1000),
        target_url=args.url,
    )


if __name__ == "__main__":
    main()
