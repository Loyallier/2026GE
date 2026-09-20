#!/usr/bin/env python3
"""
XMUM direct HTTP full-response collector.

Purpose:
- use Playwright only for manual login
- copy authenticated cookies into requests.Session
- repeatedly GET one target URL
- save the COMPLETE raw HTTP HTML response every cycle
- also store a generic dump of every HTML table for convenience
- no $All/$Page postbacks
- no course-specific parsing assumptions
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

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "collector_data"
PROFILE_DIR = PROJECT_DIR / ".http_full_profile"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)


class BrowserClosed(RuntimeError):
    pass


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


def is_login_html(html: str) -> bool:
    low = html.lower()
    return 'name="username"' in low and 'name="password"' in low


def wait_for_login_settle(page) -> None:
    print("[登录] 请在弹出的 Chromium 中手动登录。")
    last_url = ""
    stable_since = 0.0
    logged = False

    while True:
        if page.is_closed():
            raise BrowserClosed("Chromium window was closed.")

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
            print("[登录成功] 等待学校登录后的自动跳转稳定...")
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


def make_session(context) -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.4,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Referer": f"{BASE}/",
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


def extract_all_tables(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[dict[str, Any]] = []

    for ti, table in enumerate(soup.find_all("table")):
        rows: list[dict[str, Any]] = []
        for ri, tr in enumerate(table.find_all("tr")):
            cells = []
            for ci, cell in enumerate(tr.find_all(["th", "td"], recursive=False)):
                cells.append(
                    {
                        "cell_index": ci,
                        "tag": cell.name.upper(),
                        "text": cell.get_text("\n", strip=True),
                    }
                )
            rows.append({"row_index": ri, "cells": cells})

        result.append(
            {
                "table_index": ti,
                "id": table.get("id", ""),
                "class_name": " ".join(table.get("class", [])),
                "text": table.get_text("\n", strip=True),
                "rows": rows,
            }
        )

    return result


class Store:
    def __init__(self, db_path: Path, jsonl_path: Path):
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

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                local_epoch REAL NOT NULL,
                url TEXT NOT NULL,
                http_status INTEGER,
                server_time TEXT,
                elapsed_ms REAL,
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
        self.jsonl = open(jsonl_path, "a", encoding="utf-8", buffering=1)

    def save(
        self,
        *,
        captured_at: str,
        local_epoch: float,
        url: str,
        http_status: int | None,
        server_time: str | None,
        elapsed_ms: float,
        html: str,
        tables: list[dict[str, Any]],
        error: str | None,
    ) -> int:
        raw = html.encode("utf-8", errors="replace")
        sha = hashlib.sha256(raw).hexdigest()
        row_count = sum(len(t.get("rows", [])) for t in tables)
        tables_json = json.dumps(tables, ensure_ascii=False, separators=(",", ":"))

        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO html_blobs(sha256, html_gzip, raw_bytes)
                VALUES (?, ?, ?)
                """,
                (sha, gzip.compress(raw, compresslevel=6), len(raw)),
            )
            cur = self.conn.execute(
                """
                INSERT INTO snapshots(
                    captured_at, local_epoch, url, http_status, server_time,
                    elapsed_ms, html_sha256, table_count, row_count,
                    tables_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at,
                    local_epoch,
                    url,
                    http_status,
                    server_time,
                    elapsed_ms,
                    sha,
                    len(tables),
                    row_count,
                    tables_json,
                    error,
                ),
            )
            sid = int(cur.lastrowid)

        line = {
            "snapshot_id": sid,
            "captured_at": captured_at,
            "url": url,
            "http_status": http_status,
            "server_time": server_time,
            "elapsed_ms": elapsed_ms,
            "html_sha256": sha,
            "table_count": len(tables),
            "row_count": row_count,
            "error": error,
        }
        self.jsonl.write(json.dumps(line, ensure_ascii=False) + "\n")
        self.jsonl.flush()
        os.fsync(self.jsonl.fileno())
        return sid

    def close(self):
        try:
            self.jsonl.close()
        finally:
            self.conn.close()


def build_output() -> tuple[Path, Path, Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session_dir = DATA_DIR / datetime.now().strftime("httpfull_%Y%m%d_%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=True)
    return (
        session_dir,
        session_dir / "http_full.sqlite3",
        session_dir / "timeline.jsonl",
    )


def run(url: str, interval: float, timeout: float) -> None:
    session_dir, db_path, jsonl_path = build_output()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(db_path, jsonl_path)

    print(f"数据目录: {session_dir}")
    print(f"目标 URL: {url}")
    print(f"目标周期: {interval:.2f}s")
    print("模式: 浏览器只负责登录，requests 直接 GET 完整页面并保存原始 HTML。")
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
            req = make_session(context)

            probe = req.get(url, timeout=timeout)
            if "login" in probe.url.lower() or is_login_html(probe.text):
                raise RuntimeError("浏览器登录成功，但 requests 继承 Cookie 后仍被要求登录")

            print(
                f"[HTTP 就绪] {probe.status_code} | "
                f"{len(probe.content)} bytes | {probe.url}"
            )
            print("\n开始直接 HTTP 全量采集。\n")

            while True:
                cycle_started = time.perf_counter()
                captured_at = now_iso()
                local_epoch = time.time()

                status = None
                server_time = None
                html = ""
                tables: list[dict[str, Any]] = []
                error = None

                try:
                    resp = req.get(url, timeout=timeout)
                    status = resp.status_code
                    server_time = parse_server_time(resp.headers)
                    html = resp.text

                    if "login" in resp.url.lower() or is_login_html(html):
                        error = "requests session redirected to login"
                    else:
                        tables = extract_all_tables(html)

                except Exception as exc:
                    error = str(exc)

                elapsed_ms = (time.perf_counter() - cycle_started) * 1000.0

                sid = store.save(
                    captured_at=captured_at,
                    local_epoch=local_epoch,
                    url=url,
                    http_status=status,
                    server_time=server_time,
                    elapsed_ms=elapsed_ms,
                    html=html,
                    tables=tables,
                    error=error,
                )

                row_count = sum(len(t.get("rows", [])) for t in tables)

                if error:
                    print(
                        f"[{captured_at}] #{sid} FAIL "
                        f"HTTP={status or '?'} {elapsed_ms:.0f}ms | {error}"
                    )
                else:
                    print(
                        f"[{captured_at}] #{sid} OK "
                        f"HTTP={status} {elapsed_ms:.0f}ms "
                        f"tables={len(tables)} rows={row_count} "
                        f"bytes={len(html.encode('utf-8', errors='replace'))}"
                    )

                elapsed = time.perf_counter() - cycle_started
                time.sleep(max(0.0, interval - elapsed))

    except KeyboardInterrupt:
        print("\nCtrl+C：停止 HTTP 全量采集。")
    except BrowserClosed:
        print("\nChromium 已关闭，停止采集。")
    finally:
        store.close()
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        print(f"SQLite: {db_path}")
        print(f"时间线:  {jsonl_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="XMUM 登录后 requests 高频 GET + 完整 HTML 保存采集器"
    )
    parser.add_argument("--url", required=True, help="要持续 GET 的真实课程页面 URL")
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="每轮目标周期秒数，默认 3s（含请求耗时）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="单次 HTTP 请求超时秒数，默认 10s",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.interval < 0:
        raise SystemExit("--interval must be >= 0")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be > 0")
    run(args.url, args.interval, args.timeout)


if __name__ == "__main__":
    main()
