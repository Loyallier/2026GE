#!/usr/bin/env python3
"""
Analyze XMUM round-1 snapshot SQLite data and produce:
- machine-readable JSON report
- CSV time series / course summary / change events
- static PNG charts
- a self-contained-ish HTML summary (references local PNG files)

Works with:
1) a stopped standalone snapshots.sqlite3
2) a live WAL-mode snapshots.sqlite3 while round1_collector.py is still writing

The analyzer first uses SQLite's backup API to take a consistent read snapshot,
so analysis never blocks or mutates the collector database.
"""

from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

COURSE_CODE_RE = re.compile(r"^[A-Za-z]{2,}\d+[A-Za-z0-9*_-]*$")

CODE_ALIASES = {"course code", "code"}
NAME_ALIASES = {
    "course information (by group)",
    "course information",
    "course name",
    "name",
    "course",
    "waiting list",
}
QUOTA_ALIASES = {"quota", "limit", "capacity", "limitation"}
APPLICANT_ALIASES = {
    "applicant",
    "applicants",
    "applicant no.",
    "applicant no",
    "application no.",
    "application no",
    "enrolled",
    "enrolment",
    "enrollment",
    "current",
}
OPTION_ALIASES = {"option", "status"}


def norm(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("\n", " ").split())


def maybe_int(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.search(r"-?\d+", str(text))
    return int(m.group()) if m else None


def find_header(headers: list[str], aliases: set[str]) -> int | None:
    hs = [norm(x) for x in headers]
    for i, h in enumerate(hs):
        if h in aliases:
            return i
    for i, h in enumerate(hs):
        if any(a in h for a in aliases):
            return i
    return None


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def safe_mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def safe_median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def take_consistent_snapshot(source: Path) -> sqlite3.Connection:
    """
    Copy live SQLite + active WAL into an in-memory DB using SQLite backup API.
    """
    src = sqlite3.connect(
        f"file:{source.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    dst = sqlite3.connect(":memory:")
    try:
        src.backup(dst, pages=512, sleep=0.02)
    finally:
        src.close()
    return dst


def validate_schema(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required = {"snapshots", "html_blobs"}
    missing = required - tables
    if missing:
        raise SystemExit(
            f"unsupported DB: missing tables {sorted(missing)}; "
            "expected round1_collector snapshots.sqlite3"
        )


def table_rows(table: dict[str, Any]) -> list[list[str]]:
    out: list[list[str]] = []
    for row in table.get("rows", []):
        cells = row.get("cells", [])
        out.append([str(c.get("text", "")).strip() for c in cells])
    return out


def parse_courses_from_tables(
    tables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Header-driven parser that merges both available and selected tables.

    It intentionally avoids fixed table indices. Course identity is
    (course code, group/name). Applicant is only populated when the page
    actually contains an Applicant/Enrolled-like header.
    """
    merged: dict[str, dict[str, Any]] = {}

    for table in tables:
        rows = table_rows(table)
        if not rows:
            continue

        headers = rows[0]
        if not headers:
            continue

        code_i = find_header(headers, CODE_ALIASES)
        if code_i is None:
            continue

        name_i = find_header(headers, NAME_ALIASES)
        if name_i is None and code_i + 1 < len(headers):
            # XMUM's selected-course table has historically used a misleading
            # header for the group/name column. Adjacent-to-code is the safest
            # generic fallback once a real Course Code header is present.
            name_i = code_i + 1

        if name_i is None:
            continue

        quota_i = find_header(headers, QUOTA_ALIASES)
        applicant_i = find_header(headers, APPLICANT_ALIASES)
        option_i = find_header(headers, OPTION_ALIASES)

        header_text = " | ".join(norm(x) for x in headers)
        table_id = str(table.get("id", ""))
        state = (
            "selected"
            if (
                table_id == "data_table2"
                or "selected" in header_text
                or "wishing" in header_text
                or "cancel" in header_text
            )
            else "available"
        )

        for cells in rows[1:]:
            if max(code_i, name_i) >= len(cells):
                continue

            code = cells[code_i].strip()
            name = cells[name_i].strip()

            if not COURSE_CODE_RE.match(code):
                continue
            if not name:
                continue

            def get(i: int | None) -> str:
                return cells[i].strip() if i is not None and i < len(cells) else ""

            row = {
                "code": code,
                "name": name,
                "course_key": f"{code}|{name}",
                "quota": maybe_int(get(quota_i)),
                "applicant": maybe_int(get(applicant_i)),
                "state": state,
                "option": get(option_i),
                "table_id": table_id,
                "table_index": table.get("table_index"),
            }

            key = row["course_key"]
            old = merged.get(key)
            # Prefer selected copy if both tables contain the same course.
            if old is None or state == "selected":
                merged[key] = row

    return list(merged.values())


def load_observations(conn: sqlite3.Connection):
    snapshots = conn.execute(
        """
        SELECT id, captured_at, local_epoch, url, http_status,
               server_date, load_ms, html_sha256,
               table_count, row_count, tables_json, error
        FROM snapshots
        ORDER BY id
        """
    ).fetchall()

    if not snapshots:
        raise SystemExit("database contains no snapshots")

    parsed_cache: dict[str, list[dict[str, Any]]] = {}
    observations: list[dict[str, Any]] = []
    snapshot_meta: list[dict[str, Any]] = []

    t0 = float(snapshots[0][2])

    for row in snapshots:
        (
            sid,
            captured_at,
            local_epoch,
            url,
            http_status,
            server_date,
            load_ms,
            sha,
            table_count,
            row_count,
            tables_json,
            error,
        ) = row

        if sha not in parsed_cache:
            try:
                tables = json.loads(tables_json)
            except Exception:
                tables = []
            parsed_cache[sha] = parse_courses_from_tables(tables)

        courses = parsed_cache[sha]
        snapshot_meta.append(
            {
                "snapshot_id": sid,
                "captured_at": captured_at,
                "local_epoch": local_epoch,
                "elapsed_s": float(local_epoch) - t0,
                "url": url,
                "http_status": http_status,
                "server_date": server_date,
                "load_ms": load_ms,
                "html_sha256": sha,
                "table_count": table_count,
                "row_count": row_count,
                "course_count": len(courses),
                "error": error,
            }
        )

        for c in courses:
            observations.append(
                {
                    "snapshot_id": sid,
                    "captured_at": captured_at,
                    "local_epoch": float(local_epoch),
                    "elapsed_s": float(local_epoch) - t0,
                    **c,
                }
            )

    return snapshot_meta, observations, len(parsed_cache)


def value_at_or_before(points: list[dict[str, Any]], epoch: float):
    chosen = None
    for p in points:
        if p["local_epoch"] <= epoch:
            chosen = p
        else:
            break
    return chosen


def summarize_courses(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obs in observations:
        by_course[obs["course_key"]].append(obs)

    summaries: list[dict[str, Any]] = []

    for key, pts in sorted(by_course.items()):
        pts.sort(key=lambda x: x["local_epoch"])
        numeric = [p for p in pts if p["applicant"] is not None]
        quotas = [p["quota"] for p in pts if p["quota"] is not None]

        first_app = numeric[0]["applicant"] if numeric else None
        last_app = numeric[-1]["applicant"] if numeric else None
        min_app = min((p["applicant"] for p in numeric), default=None)
        max_app = max((p["applicant"] for p in numeric), default=None)

        change_events = 0
        peak_up = None
        peak_down = None
        prev = None
        for p in numeric:
            if prev is not None and p["applicant"] != prev["applicant"]:
                delta = p["applicant"] - prev["applicant"]
                change_events += 1
                if peak_up is None or delta > peak_up["delta"]:
                    peak_up = {
                        "delta": delta,
                        "captured_at": p["captured_at"],
                    }
                if peak_down is None or delta < peak_down["delta"]:
                    peak_down = {
                        "delta": delta,
                        "captured_at": p["captured_at"],
                    }
            prev = p

        recent = {}
        if numeric:
            end_epoch = numeric[-1]["local_epoch"]
            for window_s in (60, 300, 900):
                base = value_at_or_before(numeric, end_epoch - window_s)
                recent[f"delta_{window_s}s"] = (
                    last_app - base["applicant"]
                    if base is not None and last_app is not None
                    else None
                )
        else:
            recent = {"delta_60s": None, "delta_300s": None, "delta_900s": None}

        states = [p["state"] for p in pts]
        selected_transitions = 0
        prev_state = None
        for state in states:
            if prev_state is not None and state != prev_state:
                selected_transitions += 1
            prev_state = state

        quota = quotas[-1] if quotas else None
        ratio = (
            (last_app / quota)
            if last_app is not None and quota not in (None, 0)
            else None
        )

        duration_s = pts[-1]["local_epoch"] - pts[0]["local_epoch"]
        net = (
            last_app - first_app
            if first_app is not None and last_app is not None
            else None
        )
        avg_rate_per_min = (
            net / duration_s * 60.0
            if net is not None and duration_s > 0
            else None
        )

        summaries.append(
            {
                "course_key": key,
                "code": pts[-1]["code"],
                "name": pts[-1]["name"],
                "quota": quota,
                "first_applicant": first_app,
                "last_applicant": last_app,
                "min_applicant": min_app,
                "max_applicant": max_app,
                "net_change": net,
                "applicant_quota_ratio": ratio,
                "avg_net_rate_per_min": avg_rate_per_min,
                "change_event_count": change_events,
                "peak_positive_jump": peak_up,
                "peak_negative_jump": peak_down,
                "first_seen": pts[0]["captured_at"],
                "last_seen": pts[-1]["captured_at"],
                "last_state": pts[-1]["state"],
                "selected_seen": "selected" in states,
                "state_transition_count": selected_transitions,
                **recent,
            }
        )

    return summaries


def build_change_events(observations: list[dict[str, Any]]):
    by_snapshot: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for obs in observations:
        if obs["applicant"] is not None:
            by_snapshot[obs["snapshot_id"]].append(obs)

    previous: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []

    for sid in sorted(by_snapshot):
        rows = by_snapshot[sid]
        if not rows:
            continue

        changes = []
        current = {r["course_key"]: r for r in rows}

        for key, row in current.items():
            old = previous.get(key)
            if old is None:
                continue
            if old["applicant"] != row["applicant"]:
                changes.append(
                    {
                        "course_key": key,
                        "code": row["code"],
                        "name": row["name"],
                        "old": old["applicant"],
                        "new": row["applicant"],
                        "delta": row["applicant"] - old["applicant"],
                        "state": row["state"],
                    }
                )

        if changes:
            events.append(
                {
                    "snapshot_id": sid,
                    "captured_at": rows[0]["captured_at"],
                    "local_epoch": rows[0]["local_epoch"],
                    "elapsed_s": rows[0]["elapsed_s"],
                    "changed_course_count": len(changes),
                    "net_delta": sum(c["delta"] for c in changes),
                    "absolute_delta": sum(abs(c["delta"]) for c in changes),
                    "changes": changes,
                }
            )

        # Preserve previous values for courses that moved between available and
        # selected tables; update only keys visible in this snapshot.
        previous.update(current)

    return events


def build_state_events(observations: list[dict[str, Any]]):
    by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obs in observations:
        by_course[obs["course_key"]].append(obs)

    events = []
    for key, pts in by_course.items():
        pts.sort(key=lambda x: x["local_epoch"])
        prev = None
        for p in pts:
            if prev is not None and p["state"] != prev["state"]:
                events.append(
                    {
                        "course_key": key,
                        "code": p["code"],
                        "name": p["name"],
                        "captured_at": p["captured_at"],
                        "elapsed_s": p["elapsed_s"],
                        "from": prev["state"],
                        "to": p["state"],
                    }
                )
            prev = p
    events.sort(key=lambda x: x["captured_at"])
    return events


def refresh_wave_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    if len(events) < 2:
        return {
            "wave_count": len(events),
            "gap_count": 0,
            "median_gap_s": None,
            "mean_gap_s": None,
            "min_gap_s": None,
            "max_gap_s": None,
            "gaps_s": [],
        }

    gaps = [
        events[i]["local_epoch"] - events[i - 1]["local_epoch"]
        for i in range(1, len(events))
    ]
    return {
        "wave_count": len(events),
        "gap_count": len(gaps),
        "median_gap_s": safe_median(gaps),
        "mean_gap_s": safe_mean(gaps),
        "min_gap_s": min(gaps),
        "max_gap_s": max(gaps),
        "gaps_s": gaps,
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            flat = dict(row)
            for k, v in list(flat.items()):
                if isinstance(v, (dict, list)):
                    flat[k] = json.dumps(v, ensure_ascii=False)
            w.writerow(flat)


def make_charts(
    out_dir: Path,
    observations: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[str]:
    try:
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"[chart] matplotlib unavailable: {exc}")
        return []

    files: list[str] = []

    by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in observations:
        if o["applicant"] is not None:
            by_course[o["course_key"]].append(o)

    if by_course:
        fig, ax = plt.subplots(figsize=(12, 7))
        for key, pts in sorted(by_course.items()):
            pts.sort(key=lambda x: x["elapsed_s"])
            x = [p["elapsed_s"] / 60.0 for p in pts]
            y = [p["applicant"] for p in pts]
            label = f"{pts[-1]['code']} {pts[-1]['name']}"
            ax.step(x, y, where="post", label=label)
        ax.set_xlabel("Elapsed minutes")
        ax.set_ylabel("Applicants")
        ax.set_title("Applicant count over time")
        ax.grid(True, alpha=0.25)
        ax.legend(fontsize=8, loc="best")
        fig.tight_layout()
        name = "applicant_trends.png"
        fig.savefig(out_dir / name, dpi=160)
        plt.close(fig)
        files.append(name)

    ratio_rows = [
        s
        for s in summaries
        if s["last_applicant"] is not None and s["quota"] not in (None, 0)
    ]
    if ratio_rows:
        ratio_rows.sort(key=lambda x: x["applicant_quota_ratio"], reverse=True)
        labels = [f"{s['code']} {s['name']}" for s in ratio_rows]
        vals = [s["applicant_quota_ratio"] * 100 for s in ratio_rows]

        fig, ax = plt.subplots(figsize=(12, max(5, len(labels) * 0.55)))
        ypos = list(range(len(labels)))
        ax.barh(ypos, vals)
        ax.set_yticks(ypos)
        ax.set_yticklabels(labels, fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel("Applicants / quota (%)")
        ax.set_title("Latest demand relative to quota")
        ax.grid(True, axis="x", alpha=0.25)
        fig.tight_layout()
        name = "latest_demand_ratio.png"
        fig.savefig(out_dir / name, dpi=160)
        plt.close(fig)
        files.append(name)

    if events:
        fig, ax = plt.subplots(figsize=(12, 5))
        x = [e["elapsed_s"] / 60.0 for e in events]
        y = [e["absolute_delta"] for e in events]
        ax.bar(x, y, width=0.03 if len(x) > 1 else 0.2)
        ax.set_xlabel("Elapsed minutes")
        ax.set_ylabel("Total absolute applicant change")
        ax.set_title("Detected applicant-change waves")
        ax.grid(True, axis="y", alpha=0.25)
        fig.tight_layout()
        name = "change_waves.png"
        fig.savefig(out_dir / name, dpi=160)
        plt.close(fig)
        files.append(name)

    return files


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def make_html(
    out_dir: Path,
    report: dict[str, Any],
    chart_files: list[str],
) -> None:
    meta = report["metadata"]
    waves = report["refresh_waves"]
    summaries = report["course_summary"]

    rows = []
    for s in sorted(
        summaries,
        key=lambda x: (
            x["last_applicant"] is None,
            -(x["applicant_quota_ratio"] or -1),
        ),
    ):
        ratio = (
            f"{s['applicant_quota_ratio'] * 100:.1f}%"
            if s["applicant_quota_ratio"] is not None
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{html_lib.escape(s['code'])}</td>"
            f"<td>{html_lib.escape(s['name'])}</td>"
            f"<td>{fmt(s['quota'])}</td>"
            f"<td>{fmt(s['first_applicant'])}</td>"
            f"<td>{fmt(s['last_applicant'])}</td>"
            f"<td>{fmt(s['net_change'])}</td>"
            f"<td>{ratio}</td>"
            f"<td>{fmt(s['delta_60s'])}</td>"
            f"<td>{fmt(s['delta_300s'])}</td>"
            f"<td>{html_lib.escape(s['last_state'])}</td>"
            "</tr>"
        )

    imgs = "\n".join(
        f'<section><img src="{html_lib.escape(name)}" '
        'style="max-width:100%;height:auto"></section>'
        for name in chart_files
    )

    body = f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>XMUM Round 1 Analysis</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;line-height:1.45}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{border:1px solid #ddd;border-radius:8px;padding:12px 16px;min-width:160px}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border-bottom:1px solid #ddd;padding:7px;text-align:left}}
th{{position:sticky;top:0;background:#fff}}
small{{color:#666}}
</style>
<h1>XMUM Round 1 Analysis</h1>
<small>Generated {html_lib.escape(report['generated_at'])}</small>
<div class="cards">
  <div class="card"><b>Snapshots</b><br>{meta['snapshot_count']}</div>
  <div class="card"><b>Duration</b><br>{meta['duration_s']:.1f}s</div>
  <div class="card"><b>Median sample interval</b><br>{fmt(meta['median_sample_interval_s'])}s</div>
  <div class="card"><b>Courses seen</b><br>{meta['course_count']}</div>
  <div class="card"><b>Change waves</b><br>{waves['wave_count']}</div>
  <div class="card"><b>Median wave gap</b><br>{fmt(waves['median_gap_s'])}s</div>
</div>
<h2>Course summary</h2>
<table>
<thead><tr>
<th>Code</th><th>Course</th><th>Quota</th><th>First</th><th>Last</th>
<th>Net Δ</th><th>Demand/Quota</th><th>Δ 60s</th><th>Δ 300s</th><th>State</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<h2>Charts</h2>
{imgs}
</html>
"""
    (out_dir / "report.html").write_text(body, encoding="utf-8")


def analyze(source: Path, out_dir: Path) -> None:
    conn = take_consistent_snapshot(source)
    try:
        validate_schema(conn)
        snapshot_meta, observations, unique_html = load_observations(conn)
    finally:
        conn.close()

    times = [m["local_epoch"] for m in snapshot_meta]
    intervals = [b - a for a, b in zip(times, times[1:])]
    errors = [m for m in snapshot_meta if m["error"]]

    summaries = summarize_courses(observations)
    change_events = build_change_events(observations)
    state_events = build_state_events(observations)
    waves = refresh_wave_summary(change_events)

    metadata = {
        "source_database": str(source.resolve()),
        "snapshot_count": len(snapshot_meta),
        "start_time": snapshot_meta[0]["captured_at"],
        "end_time": snapshot_meta[-1]["captured_at"],
        "duration_s": times[-1] - times[0] if len(times) > 1 else 0.0,
        "mean_sample_interval_s": safe_mean(intervals),
        "median_sample_interval_s": safe_median(intervals),
        "min_sample_interval_s": min(intervals) if intervals else None,
        "max_sample_interval_s": max(intervals) if intervals else None,
        "unique_html_count": unique_html,
        "error_snapshot_count": len(errors),
        "course_count": len(summaries),
        "snapshots_with_applicant_data": len(
            {
                o["snapshot_id"]
                for o in observations
                if o["applicant"] is not None
            }
        ),
    }

    report = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "metadata": metadata,
        "refresh_waves": waves,
        "course_summary": summaries,
        "change_events": change_events,
        "state_events": state_events,
        "limitations": [
            "Applicant trends are only available when the captured page contained an Applicant/Enrolled-like column.",
            "A course moving between available and selected tables is treated as the same course when code and group/name match.",
            "Change waves are observed display-state changes, not guaranteed backend transaction times.",
        ],
    }

    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    write_csv(
        out_dir / "course_timeseries.csv",
        observations,
        [
            "snapshot_id",
            "captured_at",
            "local_epoch",
            "elapsed_s",
            "course_key",
            "code",
            "name",
            "quota",
            "applicant",
            "state",
            "option",
            "table_id",
            "table_index",
        ],
    )

    write_csv(
        out_dir / "course_summary.csv",
        summaries,
        [
            "course_key",
            "code",
            "name",
            "quota",
            "first_applicant",
            "last_applicant",
            "min_applicant",
            "max_applicant",
            "net_change",
            "applicant_quota_ratio",
            "avg_net_rate_per_min",
            "change_event_count",
            "delta_60s",
            "delta_300s",
            "delta_900s",
            "first_seen",
            "last_seen",
            "last_state",
            "selected_seen",
            "state_transition_count",
            "peak_positive_jump",
            "peak_negative_jump",
        ],
    )

    flat_events = []
    for e in change_events:
        for c in e["changes"]:
            flat_events.append(
                {
                    "snapshot_id": e["snapshot_id"],
                    "captured_at": e["captured_at"],
                    "elapsed_s": e["elapsed_s"],
                    "changed_course_count": e["changed_course_count"],
                    "wave_net_delta": e["net_delta"],
                    "wave_absolute_delta": e["absolute_delta"],
                    **c,
                }
            )

    write_csv(
        out_dir / "change_events.csv",
        flat_events,
        [
            "snapshot_id",
            "captured_at",
            "elapsed_s",
            "changed_course_count",
            "wave_net_delta",
            "wave_absolute_delta",
            "course_key",
            "code",
            "name",
            "old",
            "new",
            "delta",
            "state",
        ],
    )

    write_csv(
        out_dir / "state_events.csv",
        state_events,
        [
            "course_key",
            "code",
            "name",
            "captured_at",
            "elapsed_s",
            "from",
            "to",
        ],
    )

    charts = make_charts(out_dir, observations, summaries, change_events)
    make_html(out_dir, report, charts)

    print(f"Analysis complete: {out_dir}")
    print(f"  report.json          machine-readable full summary")
    print(f"  report.html          human-readable dashboard")
    print(f"  course_timeseries.csv all parsed observations")
    print(f"  course_summary.csv   one row per course")
    print(f"  change_events.csv    applicant changes only")
    print(f"  state_events.csv     available/selected transitions")
    if charts:
        print(f"  charts: {', '.join(charts)}")

    print(
        f"\nSnapshots={metadata['snapshot_count']} "
        f"courses={metadata['course_count']} "
        f"applicant_snapshots={metadata['snapshots_with_applicant_data']} "
        f"change_waves={waves['wave_count']}"
    )
    if waves["median_gap_s"] is not None:
        print(f"Median observed change-wave gap: {waves['median_gap_s']:.2f}s")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze round1_collector SQLite snapshots and generate trends/report."
    )
    parser.add_argument("database", type=Path, help="snapshots.sqlite3 (live or stopped)")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output directory; default: <database-dir>/analysis_<timestamp>",
    )
    args = parser.parse_args()

    source = args.database
    if not source.exists():
        raise SystemExit(f"database not found: {source}")

    out = args.out or (
        source.parent / datetime.now().strftime("analysis_%Y%m%d_%H%M%S")
    )
    analyze(source, out)


if __name__ == "__main__":
    main()
