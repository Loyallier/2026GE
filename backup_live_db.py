#!/usr/bin/env python3
"""
Create a portable standalone copy of a live SQLite database.

SQLite backup API reads the main DB + active WAL consistently while another
process is writing, and produces one self-contained destination .sqlite3 file.

Usage:
    python backup_live_db.py SOURCE.sqlite3 DEST.sqlite3
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def backup(source: Path, dest: Path) -> None:
    if not source.exists():
        raise SystemExit(f"source not found: {source}")

    dest.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)

    try:
        with dst:
            src.backup(dst, pages=256, sleep=0.05)
        row = dst.execute(
            "SELECT COUNT(*), MAX(id), MAX(captured_at) FROM snapshots"
        ).fetchone()
        print(f"backup complete: {dest}")
        if row:
            print(f"snapshots={row[0]} max_id={row[1]} latest={row[2]}")
    finally:
        dst.close()
        src.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safely copy a live WAL-mode SQLite DB into one portable file."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path)
    args = parser.parse_args()
    backup(args.source, args.dest)


if __name__ == "__main__":
    main()
