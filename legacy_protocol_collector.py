#!/usr/bin/env python3
"""
Legacy XMUM Random-round protocol collector.

This intentionally preserves the proven 2025/2026 protocol used by
ac_sniper_V2.py / ac_checkpoints_v2.py:

    GET Random page
      -> extract __VIEWSTATE
      -> POST $All
      -> parse page 1
      -> extract new __VIEWSTATE
      -> POST $Page / $2 (optional)
      -> parse page 2

Changes versus the old scripts:
- manual browser login instead of hard-coded Cookie
- browser cookies copied into requests.Session
- fixed high-frequency loop instead of prediction/scheduling
- SQLite + raw HTML storage instead of only CSV
- old table/column parser kept intentionally as a compatibility fallback
"""

from __future__ import annotations

import argparse
import email.utils
import gzip
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://ac.xmu.edu.my"
LOGIN_URL = f"{BASE}/index.php"
DEFAULT_RANDOM_URL = f"{BASE}/student/index.php?c=Xk&a=Random&id=1402"

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "collector_data"
PROFILE_DIR = PROJECT_DIR / ".legacy_protocol_profile"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def parse_server_time(headers: dict[str, str]) -> str | None:
    date_str = headers.get("Date") or headers.get("date")
    if not date_str:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        return dt.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds")
    except Exception:
        return None


def is_login_html(html: str) -> bool:
    low = html.lower()
    return 'name="username"' in low and 'name="password"' in low


def is_login_page(page) -> bool:
    try:
        if "c=login" in page.url.lower():
            return True
        return (
            page.locator('input[name="username"]').count() > 0
            and page.locator('input[name="password"]').count() > 0
        )
    except Exception:
        return False


def wait_for_login_settle(page) -> None:
    """Wait until login form disappears and XMUM redirect chain settles."""
    print("[登录] 请在弹出的 Chromium 中手动登录。")
    last_url = ""
    stable_since = 0.0
    logged = False

    while True:
        if page.is_closed():
            raise KeyboardInterrupt

        try:
            on_login = is_login_page(page)
            current = page.url
        except Exception:
            time.sleep(0.2)
            continue

        now = time.monotonic()
        if on_login:
            logged = False
            stable_since = 0.0
            time.sleep(0.3)
            continue

        if not logged:
            print("[登录] 表单已消失，等待学校自动跳转稳定...")
            logged = True
            last_url = current
            stable_since = now

        if current != last_url:
            print(f"[登录跳转] {current}")
            last_url = current
            stable_since = now

        if now - stable_since >= 1.5:
            print(f"[登录就绪] {current}")
            return

        time.sleep(0.2)


def make_requests_session(context) -> requests.Session:
    """Copy Playwright cookies into requests.Session."""
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Referer": f"{BASE}/",
            "Origin": BASE,
            "Connection": "keep-alive",
        }
    )

    for cookie in context.cookies():
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain") or "ac.xmu.edu.my",
            path=cookie.get("path") or "/",
        )

    return session


def extract_viewstate(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("input", {"name": "__VIEWSTATE"})
    return node.get("value") if node else None


def extract_old_style(html: str, page_num: int, captured_at: str, server_time: str | None):
    """
    Preserve the old stable parser intentionally:
    scan table[2] and table[3], columns 0/1/7/8.
    """
    soup = BeautifulSoup(html, "html.parser")
    compare: dict[str, str] = {}
    records: list[dict[str, Any]] = []

    tables = soup.find_all("table")
    for idx in (2, 3):
        if idx >= len(tables):
            continue

        for row in tables[idx].find_all("tr"):
            cols = row.find_all(["td", "th"])
            if len(cols) < 9:
                continue

            try:
                code = cols[0].get_text(strip=True)
                name = cols[1].get_text(strip=True)
                limit_num = cols[7].get_text(strip=True)
                current_num = cols[8].get_text(strip=True)

                if code.lower() == "code" or not current_num.isdigit():
                    continue

                key = f"{code}|{name}"
                compare[key] = current_num
                records.append(
                    {
                        "captured_at": captured_at,
                        "server_time": server_time,
                        "page": page_num,
                        "code": code,
                        "name": name,
                        "enrolled": int(current_num),
                        "limit": limit_num,
                    }
                )
            except Exception:
                continue

    return compare, records


class Store:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS html_blobs (
                sha256 TEXT PRIMARY KEY,
                html_gzip BLOB NOT NULL,
                raw_bytes INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS protocol_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                elapsed_ms REAL,
                server_time TEXT,
                success INTEGER NOT NULL,
                course_count INTEGER NOT NULL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS protocol_courses (
                snapshot_id INTEGER NOT NULL,
                page INTEGER NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                enrolled INTEGER NOT NULL,
                limit_text TEXT,
                FOREIGN KEY(snapshot_id) REFERENCES protocol_snapshots(id)
            );

            CREATE TABLE IF NOT EXISTS protocol_responses (
                snapshot_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                page INTEGER,
                url TEXT NOT NULL,
                http_status INTEGER,
                server_time TEXT,
                html_sha256 TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES protocol_snapshots(id),
                FOREIGN KEY(html_sha256) REFERENCES html_blobs(sha256)
            );

            CREATE INDEX IF NOT EXISTS idx_protocol_snapshots_time
            ON protocol_snapshots(captured_at);

            CREATE INDEX IF NOT EXISTS idx_protocol_courses_snapshot
            ON protocol_courses(snapshot_id);
            """
        )
        self.conn.commit()

    def _put_html(self, html: str) -> str:
        raw = html.encode("utf-8", errors="replace")
        sha = hashlib.sha256(raw).hexdigest()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO html_blobs(sha256, html_gzip, raw_bytes)
            VALUES (?, ?, ?)
            """,
            (sha, gzip.compress(raw, compresslevel=6), len(raw)),
        )
        return sha

    def save_cycle(
        self,
        *,
        captured_at: str,
        elapsed_ms: float,
        server_time: str | None,
        success: bool,
        courses: list[dict[str, Any]],
        responses: list[dict[str, Any]],
        error: str | None,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO protocol_snapshots(
                    captured_at, elapsed_ms, server_time,
                    success, course_count, error
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at,
                    elapsed_ms,
                    server_time,
                    1 if success else 0,
                    len(courses),
                    error,
                ),
            )
            sid = int(cur.lastrowid)

            for row in courses:
                self.conn.execute(
                    """
                    INSERT INTO protocol_courses(
                        snapshot_id, page, code, name, enrolled, limit_text
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sid,
                        row["page"],
                        row["code"],
                        row["name"],
                        row["enrolled"],
                        row["limit"],
                    ),
                )

            for resp in responses:
                sha = self._put_html(resp["html"])
                self.conn.execute(
                    """
                    INSERT INTO protocol_responses(
                        snapshot_id, stage, page, url,
                        http_status, server_time, html_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sid,
                        resp["stage"],
                        resp.get("page"),
                        resp["url"],
                        resp.get("status"),
                        resp.get("server_time"),
                        sha,
                    ),
                )

        return sid

    def close(self):
        self.conn.close()


def response_record(stage: str, response: requests.Response, page: int | None = None):
    return {
        "stage": stage,
        "page": page,
        "url": response.url,
        "status": response.status_code,
        "server_time": parse_server_time(response.headers),
        "html": response.text,
    }


def fetch_legacy_cycle(
    session: requests.Session,
    base_url: str,
    pages: int,
    timeout: float,
):
    """
    Old protocol, with raw-response retention.

    pages=1:
      GET -> POST $All
    pages>=2:
      GET -> POST $All -> POST $Page/$2 -> ...
    """
    started = time.perf_counter()
    captured_at = now_iso()
    responses: list[dict[str, Any]] = []
    all_compare: dict[str, str] = {}
    all_records: list[dict[str, Any]] = []
    last_server_time: str | None = None

    try:
        resp = session.get(base_url, timeout=timeout)
        responses.append(response_record("GET", resp))

        if "login" in resp.url.lower() or is_login_html(resp.text):
            raise RuntimeError("requests session was redirected to login")

        vs = extract_viewstate(resp.text)
        if not vs:
            raise RuntimeError("__VIEWSTATE not found on Random page")

        resp1 = session.post(
            base_url,
            data={
                "__EVENTTARGET": "$All",
                "__EVENTARGUMENT": "",
                "__VIEWSTATE": vs,
            },
            timeout=timeout,
        )
        srv1 = parse_server_time(resp1.headers)
        responses.append(response_record("ALL", resp1, 1))
        comp1, rec1 = extract_old_style(resp1.text, 1, captured_at, srv1)
        all_compare.update(comp1)
        all_records.extend(rec1)
        last_server_time = srv1

        if pages > 1:
            vs = extract_viewstate(resp1.text)
            if not vs:
                raise RuntimeError("__VIEWSTATE missing after $All")

            for page_num in range(2, pages + 1):
                # Preserve the stable V2 postback convention.
                respn = session.post(
                    base_url,
                    data={
                        "__EVENTTARGET": "$Page",
                        "__EVENTARGUMENT": f"$%d" % page_num,
                        "__VIEWSTATE": vs,
                    },
                    timeout=timeout,
                )
                srvn = parse_server_time(respn.headers)
                responses.append(response_record("PAGE", respn, page_num))
                compn, recn = extract_old_style(respn.text, page_num, captured_at, srvn)
                all_compare.update(compn)
                all_records.extend(recn)
                last_server_time = srvn

                new_vs = extract_viewstate(respn.text)
                if new_vs:
                    vs = new_vs

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "captured_at": captured_at,
            "elapsed_ms": elapsed_ms,
            "server_time": last_server_time,
            "compare": all_compare,
            "records": all_records,
            "responses": responses,
            "error": None,
        }

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "captured_at": captured_at,
            "elapsed_ms": elapsed_ms,
            "server_time": last_server_time,
            "compare": all_compare,
            "records": all_records,
            "responses": responses,
            "error": str(exc),
        }


def build_output() -> tuple[Path, Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session_dir = DATA_DIR / datetime.now().strftime("legacy_%Y%m%d_%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir, session_dir / "legacy_protocol.sqlite3"


def run(base_url: str, pages: int, interval: float, timeout: float) -> None:
    session_dir, db_path = build_output()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)

    print(f"数据目录: {session_dir}")
    print(f"Random URL: {base_url}")
    print(f"旧协议页数: {pages}")
    print(f"目标周期: {interval:.2f}s")
    print("此脚本保留旧 V2 的 ViewState/$All/$Page 协议逻辑。")
    print("停止: Ctrl+C\n")

    context = None

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                no_viewport=True,
                args=["--start-maximized"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_navigation_timeout(int(timeout * 1000))

            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            except PlaywrightTimeoutError:
                print("[登录] 登录页加载超时，可手动刷新。")

            wait_for_login_settle(page)

            req = make_requests_session(context)

            # Verify transferred cookies before entering the loop.
            probe = req.get(base_url, timeout=timeout)
            if "login" in probe.url.lower() or is_login_html(probe.text):
                raise RuntimeError("浏览器登录成功，但 Cookie 注入 requests 后仍被要求登录")

            print(f"[协议就绪] requests 已继承浏览器 Session: {probe.url}")
            print("\n开始旧协议高频采集。\n")

            previous: dict[str, str] = {}
            cycle = 0

            while True:
                cycle_started = time.perf_counter()
                result = fetch_legacy_cycle(req, base_url, pages, timeout)

                sid = store.save_cycle(
                    captured_at=result["captured_at"],
                    elapsed_ms=result["elapsed_ms"],
                    server_time=result["server_time"],
                    success=result["error"] is None,
                    courses=result["records"],
                    responses=result["responses"],
                    error=result["error"],
                )
                cycle += 1

                compare = result["compare"]
                changes: list[str] = []
                if compare:
                    if previous:
                        for key, value in compare.items():
                            old = previous.get(key)
                            if old is not None and old != value:
                                changes.append(f"{key}: {old}->{value}")
                    previous = dict(compare)

                if result["error"]:
                    print(
                        f"[{result['captured_at']}] #{sid} FAIL "
                        f"{result['elapsed_ms']:.0f}ms | {result['error']}"
                    )
                else:
                    print(
                        f"[{result['captured_at']}] #{sid} OK "
                        f"{result['elapsed_ms']:.0f}ms | courses={len(result['records'])} "
                        f"| server={result['server_time'] or '?'}"
                    )
                    for change in changes:
                        print(f"   CHANGE {change}")

                elapsed = time.perf_counter() - cycle_started
                time.sleep(max(0.0, interval - elapsed))

    except KeyboardInterrupt:
        print("\nCtrl+C：停止旧协议采集。")
    finally:
        store.close()
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        print(f"SQLite: {db_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="XMUM 旧 Random/ViewState 协议高频保底采集器"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_RANDOM_URL,
        help="本轮 Random 页面 URL；正式开放后请确认 id",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=2,
        help="按旧逻辑抓取页数，默认 2；若本轮只有一页可设 1",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="完整一轮的目标间隔秒数，默认 3s",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="单个 HTTP 请求超时秒数，默认 10s",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.pages < 1:
        raise SystemExit("--pages must be >= 1")
    if args.interval < 0:
        raise SystemExit("--interval must be >= 0")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be > 0")

    run(args.url, args.pages, args.interval, args.timeout)


if __name__ == "__main__":
    main()
