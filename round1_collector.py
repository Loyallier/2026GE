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
import csv
import gzip
import hashlib
import json
import os
import re
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
    """
    Wait for login AND the school's post-login redirect chain to settle.

    XMUM removes the login form before its automatic navigation to
    /student/index.php?c=Default&a=inf is finished. Returning at that exact
    moment races with page.goto(target_url), so require a stable post-login
    URL for a short period.
    """
    last_msg = 0.0
    login_disappeared = False
    last_url = ""
    stable_since = 0.0

    while True:
        if page.is_closed():
            raise BrowserClosed("Chromium window was closed.")

        try:
            on_login = is_login_page(page)
            current_url = page.url
        except Exception as exc:
            if _is_closed_error(exc):
                raise BrowserClosed("Chromium window was closed.") from exc
            time.sleep(0.2)
            continue

        now = time.monotonic()

        if on_login:
            login_disappeared = False
            stable_since = 0.0
            if now >= last_msg:
                print("[等待登录] 请在 Chromium 中完成登录...")
                last_msg = now + 5.0
            time.sleep(0.3)
            continue

        if not login_disappeared:
            print("[登录成功] 等待学校登录后的自动跳转稳定...")
            login_disappeared = True
            last_url = current_url
            stable_since = now

        if current_url != last_url:
            print(f"[登录跳转] {current_url}")
            last_url = current_url
            stable_since = now

        # Require a stable URL for 1.5s after the login page disappears.
        if now - stable_since >= 1.5:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=1500)
            except Exception:
                pass
            print(f"[登录就绪] {page.url}")
            return

        time.sleep(0.2)


def goto_with_navigation_retry(page: Page, url: str, timeout_ms: int) -> Response | None:
    """Navigate after login; tolerate XMUM finishing a competing redirect."""
    last_exc: Exception | None = None

    for attempt in range(1, 6):
        if page.is_closed():
            raise BrowserClosed("Chromium window was closed.")

        try:
            return page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            if _is_closed_error(exc):
                raise BrowserClosed("Chromium window was closed.") from exc

            last_exc = exc
            msg = str(exc).lower()
            transient = (
                "interrupted by another navigation" in msg
                or "navigation to" in msg and "interrupted" in msg
            )
            if not transient:
                raise

            print(f"[导航竞争] 学校仍在自动跳转，等待后重试 ({attempt}/5)...")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            time.sleep(0.8)

    assert last_exc is not None
    raise last_exc


def _norm_header(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("\n", " ").split())


def _find_header(headers: list[str], aliases: set[str]) -> int | None:
    normalized = [_norm_header(h) for h in headers]
    for i, h in enumerate(normalized):
        if h in aliases:
            return i
    for i, h in enumerate(normalized):
        if any(alias in h for alias in aliases):
            return i
    return None


def parse_live_courses(html: str) -> tuple[list[dict[str, Any]], bool]:
    """
    Header-driven live parser.

    Never assumes fixed table number or fixed column indices. It only reports
    applicant/enrolled counts when such a column is actually present.
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return [], False

    code_aliases = {"course code", "code"}
    name_aliases = {"course information (by group)", "course name", "name", "course"}
    quota_aliases = {"quota", "limit", "capacity", "limitation"}
    applicant_aliases = {
        "applicant", "applicants", "applicant no.", "applicant no",
        "enrolled", "enrolment", "enrollment", "current"
    }
    option_aliases = {"option", "status"}

    best: list[dict[str, Any]] = []
    best_has_applicant = False

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [cell.get_text(" ", strip=True) for cell in header_row.find_all(["th", "td"])]
        if not headers:
            continue

        code_i = _find_header(headers, code_aliases)
        name_i = _find_header(headers, name_aliases)
        quota_i = _find_header(headers, quota_aliases)
        applicant_i = _find_header(headers, applicant_aliases)
        option_i = _find_header(headers, option_aliases)

        if code_i is None or name_i is None:
            continue

        parsed: list[dict[str, Any]] = []
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"], recursive=False)
            if max(code_i, name_i) >= len(cells):
                continue

            code = cells[code_i].get_text(" ", strip=True)
            name = cells[name_i].get_text(" ", strip=True)
            if not code or not name or code.lower() in {"code", "course code"}:
                continue

            def cell_text(idx: int | None) -> str:
                return cells[idx].get_text(" ", strip=True) if idx is not None and idx < len(cells) else ""

            def maybe_int(text: str) -> int | None:
                m = re.search(r"-?\d+", text or "")
                return int(m.group()) if m else None

            parsed.append({
                "code": code,
                "name": name,
                "quota": maybe_int(cell_text(quota_i)),
                "applicant": maybe_int(cell_text(applicant_i)) if applicant_i is not None else None,
                "option": cell_text(option_i),
            })

        if len(parsed) > len(best):
            best = parsed
            best_has_applicant = applicant_i is not None

    return best, best_has_applicant

def write_live_csv(path: Path, courses: list[dict[str, Any]]) -> None:
    """Atomically replace the latest live CSV so readers never see half a file."""
    tmp = path.with_suffix(".tmp")
    fields = ["code", "name", "quota", "applicant", "option"]
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(courses)
    try:
        os.replace(tmp, path)
    except PermissionError:
        # Excel may lock live_latest.csv on Windows. Keep collection running.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def update_live_view(
    page: Page,
    session_dir: Path,
    previous: dict[str, int] | None,
    *,
    force_print: bool = False,
) -> dict[str, int] | None:
    """
    Write the latest course list. Applicant changes are shown only when the
    page really contains an Applicant/Enrolled column.
    """
    try:
        html = page.content()
        courses, has_applicant = parse_live_courses(html)
        if not courses:
            return previous

        write_live_csv(session_dir / "live_latest.csv", courses)

        if not has_applicant:
            if force_print:
                print(
                    f"\n[LIVE] 已识别 {len(courses)} 门/组课程；"
                    "当前页面没有 Applicant/Enrolled 列，只显示 Quota。"
                )
                for course in courses:
                    quota = course.get("quota")
                    print(
                        f"  {course.get('code',''):<10} "
                        f"{course.get('name','')[:56]:<56} "
                        f"Quota {quota if quota is not None else '?'}"
                    )
                print(f"[LIVE] 最新完整表: {session_dir / 'live_latest.csv'}\n")
            return previous

        current: dict[str, int] = {}
        changes: list[tuple[dict[str, Any], int | None, int]] = []

        for course in courses:
            applicant = course.get("applicant")
            if applicant is None:
                continue
            key = f"{course.get('code','')}|{course.get('name','')}"
            current[key] = int(applicant)
            old = previous.get(key) if previous else None
            if force_print or old is None or int(applicant) != old:
                changes.append((course, old, int(applicant)))

        if changes:
            print("\n[LIVE] 当前课程申请人数 / 变化")
            for course, old, applicant in changes:
                quota = course.get("quota")
                delta = "初始" if old is None else f"{applicant - old:+d}"
                print(
                    f"  {course.get('code',''):<10} "
                    f"{course.get('name','')[:52]:<52} "
                    f"申请 {applicant:>4} / "
                    f"{quota if quota is not None else '?':<4}  Δ {delta}"
                )
            print(f"[LIVE] 最新完整表: {session_dir / 'live_latest.csv'}\n")

        return current
    except Exception as exc:
        print(f"[LIVE] 实时视图解析失败（原始采集不受影响）: {exc}")
        return previous

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
                print("检测到登录流程稳定，进入目标页面...")
                goto_with_navigation_retry(page, target_url, timeout_ms)
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
            live_state: dict[str, int] | None = None
            snapshot_count = 0

            try:
                result = capture_current_page(page, store, None, None)
                snapshot_count += 1
                previous_row_count = result.row_count
                print(
                    f"[{result.captured_at}] #{result.snapshot_id} 初始页 "
                    f"tables={result.table_count} rows={result.row_count}"
                )
                live_state = update_live_view(
                    page, session_dir, live_state, force_print=True
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
                    live_state = update_live_view(page, session_dir, live_state)
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
