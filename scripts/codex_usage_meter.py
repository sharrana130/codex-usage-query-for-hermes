#!/usr/bin/env python3
"""Read the latest locally cached Codex rate-limit event.

This module intentionally does not read ~/.codex/auth.json. It only inspects
Codex log SQLite files for websocket events of type "codex.rate_limits".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable


VERSION = "0.2.0"
DEFAULT_STALE_AFTER_MINUTES = 30
DEFAULT_REFRESH_TIMEOUT_SECONDS = 180
DEFAULT_REFRESH_PROMPT = "只回复 OK，不修改文件。"
RATE_LIMIT_TYPE = "codex.rate_limits"
RESET_COUNT_KEYS = {
    "available_resets",
    "available_reset_count",
    "remaining_resets",
    "resets_remaining",
    "reset_count",
    "reset_credits",
    "resets_available",
}


class ProbeError(Exception):
    """Raised for expected probe failures."""


def _clean_snippet(text: str, limit: int = 600) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(sk-[A-Za-z0-9_-]+|sess-[A-Za-z0-9_-]+|Bearer\s+[A-Za-z0-9._-]+)", "[REDACTED]", text)
    text = re.sub(r"[A-Za-z0-9_-]{80,}", "[LONG]", text)
    return text[:limit]


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        try:
            key = str(path.expanduser().resolve())
        except OSError:
            key = str(path.expanduser())
        if key not in seen:
            seen.add(key)
            out.append(Path(key))
    return out


def discover_codex_homes() -> list[Path]:
    """Return plausible CODEX_HOME directories on Windows, Linux, and WSL."""

    candidates: list[Path] = []
    for env_name in ("CODEX_USAGE_CODEX_HOME", "CODEX_HOME"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(Path(value))

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidates.append(Path(user_profile) / ".codex")

    candidates.append(Path.home() / ".codex")

    # Common WSL case: Hermes runs in Linux, while Codex Desktop stores state on
    # the Windows filesystem.
    mnt_users = Path("/mnt/c/Users")
    if mnt_users.exists():
        user = os.environ.get("USER")
        if user:
            candidates.append(mnt_users / user / ".codex")
        try:
            candidates.extend(path / ".codex" for path in mnt_users.iterdir() if path.is_dir())
        except OSError:
            pass

    return [path for path in _dedupe_paths(candidates) if path.exists()]


def _normalize_log_ts(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:
        ts = ts / 1000.0
    return ts


def _iso_from_epoch(seconds: float | int | None, *, local: bool) -> str | None:
    if seconds is None:
        return None
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    tz = _dt.datetime.now().astimezone().tzinfo if local else _dt.timezone.utc
    return _dt.datetime.fromtimestamp(value, tz=tz).isoformat(timespec="seconds")


def _raw_decode_json_candidates(text: str) -> Iterable[dict[str, Any]]:
    decoder = json.JSONDecoder()
    starts: list[int] = []
    marker = "websocket event:"
    marker_pos = text.find(marker)
    if marker_pos >= 0:
        brace = text.find("{", marker_pos)
        if brace >= 0:
            starts.append(brace)

    type_pos = text.find(RATE_LIMIT_TYPE)
    if type_pos >= 0:
        brace = text.rfind("{", 0, type_pos)
        if brace >= 0:
            starts.append(brace)

    starts.extend(match.start() for match in re.finditer(r"\{", text))

    seen: set[int] = set()
    for start in starts:
        if start in seen:
            continue
        seen.add(start)
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _extract_rate_limit_event(text: str) -> dict[str, Any] | None:
    if RATE_LIMIT_TYPE not in text:
        return None
    for obj in _raw_decode_json_candidates(text):
        if obj.get("type") == RATE_LIMIT_TYPE:
            return obj
    return None


def _log_databases(codex_home: Path) -> list[Path]:
    paths = sorted(
        codex_home.glob("logs_*.sqlite"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return paths


def _read_events_from_db(path: Path, *, limit: int = 200) -> Iterable[dict[str, Any]]:
    uri = f"file:{path}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error as exc:
        raise ProbeError(f"Cannot open {path}: {exc}") from exc

    try:
        cols = {row[1] for row in con.execute("pragma table_info(logs)")}
        if "feedback_log_body" not in cols or "ts" not in cols:
            return
        query = (
            "select ts, feedback_log_body from logs "
            "where feedback_log_body like ? "
            "order by ts desc limit ?"
        )
        for ts, body in con.execute(query, (f"%{RATE_LIMIT_TYPE}%", limit)):
            if not body:
                continue
            event = _extract_rate_limit_event(str(body))
            if event:
                yield {
                    "event": event,
                    "log_ts": _normalize_log_ts(ts),
                    "source": str(path),
                }
    finally:
        con.close()


def find_latest_event(codex_home: Path) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for db_path in _log_databases(codex_home):
        try:
            for found in _read_events_from_db(db_path):
                if latest is None or (found.get("log_ts") or 0) > (latest.get("log_ts") or 0):
                    latest = found
                break
        except ProbeError:
            continue
    return latest


def _event_is_after(codex_home: str | Path | None, started_at: float) -> bool:
    homes = [Path(codex_home).expanduser()] if codex_home else discover_codex_homes()
    for home in homes:
        latest = find_latest_event(home)
        if latest and (latest.get("log_ts") or 0) >= started_at - 2:
            return True
    return False


def run_codex_refresh(
    *,
    codex_command: str = "codex",
    codex_home: str | Path | None = None,
    prompt: str = DEFAULT_REFRESH_PROMPT,
    timeout_seconds: int = DEFAULT_REFRESH_TIMEOUT_SECONDS,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Run a minimal Codex CLI request so Codex refreshes rate-limit logs."""

    started_at = time.time()
    command = [
        codex_command,
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        prompt,
    ]
    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = str(codex_home)

    result: dict[str, Any] = {
        "requested": True,
        "status": "unknown",
        "started_at_local": _iso_from_epoch(started_at, local=True),
        "command": command[:6] + ["<prompt>"],
    }
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=True,
            timeout=max(1, int(timeout_seconds)),
            check=False,
        )
        result.update(
            {
                "returncode": completed.returncode,
                "duration_seconds": round(time.time() - started_at, 3),
                "stdout_snippet": _clean_snippet(completed.stdout),
                "stderr_snippet": _clean_snippet(completed.stderr),
            }
        )
        if completed.returncode == 0:
            # SQLite log writes can trail process exit briefly.
            deadline = time.time() + 8
            while time.time() < deadline:
                if _event_is_after(codex_home, started_at):
                    result["status"] = "ok"
                    result["new_rate_limit_event_observed"] = True
                    break
                time.sleep(0.25)
            else:
                result["status"] = "completed_without_new_rate_limit_event"
                result["new_rate_limit_event_observed"] = False
        else:
            result["status"] = "failed"
            result["new_rate_limit_event_observed"] = False
    except FileNotFoundError as exc:
        result.update(
            {
                "status": "failed",
                "error": f"Codex command not found: {codex_command}",
                "exception": exc.__class__.__name__,
                "duration_seconds": round(time.time() - started_at, 3),
                "new_rate_limit_event_observed": False,
            }
        )
    except subprocess.TimeoutExpired as exc:
        result.update(
            {
                "status": "timeout",
                "error": f"Codex refresh timed out after {timeout_seconds} seconds.",
                "stdout_snippet": _clean_snippet(exc.stdout or ""),
                "stderr_snippet": _clean_snippet(exc.stderr or ""),
                "duration_seconds": round(time.time() - started_at, 3),
                "new_rate_limit_event_observed": False,
            }
        )
    return result


def _window_name(raw: dict[str, Any], fallback: str) -> str:
    minutes = raw.get("window_minutes")
    if minutes == 300:
        return "5_hour"
    if minutes == 10080:
        return "weekly"
    return fallback


def _build_window(raw: dict[str, Any], fallback_name: str, now: float) -> dict[str, Any]:
    used = raw.get("used_percent")
    try:
        used_number = float(used)
    except (TypeError, ValueError):
        used_number = None

    reset_at = raw.get("reset_at")
    try:
        reset_at_number = float(reset_at) if reset_at is not None else None
    except (TypeError, ValueError):
        reset_at_number = None

    remaining = None if used_number is None else max(0.0, min(100.0, 100.0 - used_number))
    seconds_until_reset = None
    reset_has_passed = None
    if reset_at_number is not None:
        seconds_until_reset = max(0, int(reset_at_number - now))
        reset_has_passed = reset_at_number <= now

    return {
        "name": _window_name(raw, fallback_name),
        "window_minutes": raw.get("window_minutes"),
        "last_observed_used_percent": used,
        "last_observed_remaining_percent": remaining,
        "reset_at_epoch_seconds": reset_at,
        "reset_at_local": _iso_from_epoch(reset_at_number, local=True),
        "reset_at_utc": _iso_from_epoch(reset_at_number, local=False),
        "seconds_until_reset": seconds_until_reset,
        "reset_has_passed": reset_has_passed,
        "reset_after_seconds_from_event": raw.get("reset_after_seconds"),
    }


def _find_reset_count(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized = str(key).lower()
            if normalized in RESET_COUNT_KEYS and isinstance(value, (int, float, str)):
                return {"key": key, "value": value}
        for value in obj.values():
            found = _find_reset_count(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_reset_count(item)
            if found:
                return found
    return None


def build_snapshot(
    codex_home: str | Path | None = None,
    *,
    stale_after_minutes: int = DEFAULT_STALE_AFTER_MINUTES,
) -> dict[str, Any]:
    homes = [Path(codex_home).expanduser()] if codex_home else discover_codex_homes()
    now = time.time()

    if not homes:
        return {
            "status": "unavailable",
            "generated_at_local": _iso_from_epoch(now, local=True),
            "error": "No Codex home directory found. Set CODEX_USAGE_CODEX_HOME or pass --codex-home.",
        }

    checked: list[str] = []
    latest: dict[str, Any] | None = None
    used_home: Path | None = None
    for home in homes:
        checked.append(str(home))
        found = find_latest_event(home)
        if found and (latest is None or (found.get("log_ts") or 0) > (latest.get("log_ts") or 0)):
            latest = found
            used_home = home

    if latest is None:
        return {
            "status": "unavailable",
            "generated_at_local": _iso_from_epoch(now, local=True),
            "checked_codex_homes": checked,
            "error": "No codex.rate_limits event found in local logs_*.sqlite files.",
        }

    event = latest["event"]
    observed_at = latest.get("log_ts")
    age_seconds = None if observed_at is None else max(0, int(now - observed_at))
    stale = age_seconds is None or age_seconds > stale_after_minutes * 60

    rate_limits = event.get("rate_limits") or {}
    windows: dict[str, Any] = {}
    for key in ("primary", "secondary"):
        raw = rate_limits.get(key)
        if isinstance(raw, dict):
            window = _build_window(raw, key, now)
            windows[window["name"]] = window

    reset_count = _find_reset_count(event)
    warnings: list[str] = []
    if stale:
        warnings.append(
            f"Snapshot is older than {stale_after_minutes} minutes; values are last observed, not guaranteed current."
        )
    if any(window.get("reset_has_passed") for window in windows.values()):
        warnings.append("At least one reset time has already passed since the snapshot was logged.")
    if reset_count is None:
        warnings.append("The latest codex.rate_limits event did not include an available reset-count field.")

    return {
        "status": "stale" if stale else "ok",
        "generated_at_local": _iso_from_epoch(now, local=True),
        "source": {
            "kind": "local_codex_log",
            "codex_home": str(used_home) if used_home else None,
            "path": latest.get("source"),
            "event_type": RATE_LIMIT_TYPE,
        },
        "plan_type": event.get("plan_type"),
        "allowed": rate_limits.get("allowed"),
        "limit_reached": rate_limits.get("limit_reached"),
        "observed_at_epoch_seconds": observed_at,
        "observed_at_local": _iso_from_epoch(observed_at, local=True),
        "observed_at_utc": _iso_from_epoch(observed_at, local=False),
        "age_seconds": age_seconds,
        "stale_after_minutes": stale_after_minutes,
        "windows": windows,
        "available_resets": {
            "value": None if reset_count is None else reset_count.get("value"),
            "source_key": None if reset_count is None else reset_count.get("key"),
            "availability": "not_exposed_in_latest_event" if reset_count is None else "observed",
        },
        "code_review_rate_limits": event.get("code_review_rate_limits"),
        "warnings": warnings,
    }


def _percent(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return f"{int(numeric)}%"
    return f"{numeric:.1f}%"


def format_markdown(snapshot: dict[str, Any]) -> str:
    lines: list[str] = ["# Codex Usage Meter", ""]
    lines.append(f"Status: {snapshot.get('status', 'unknown')}")
    refresh = snapshot.get("refresh")
    if isinstance(refresh, dict) and refresh.get("requested"):
        lines.append(f"Refresh: {refresh.get('status', 'unknown')}")
        if refresh.get("status") != "ok":
            lines.append("Snapshot type: old local log snapshot")
        elif snapshot.get("status") == "stale":
            lines.append("Snapshot type: refreshed request completed, but only stale local log data was found")
        else:
            lines.append("Snapshot type: refreshed local log snapshot")
    elif snapshot.get("status") == "stale":
        lines.append("Snapshot type: old local log snapshot")
    if snapshot.get("plan_type"):
        lines.append(f"Plan type: {snapshot['plan_type']}")
    if snapshot.get("observed_at_local"):
        lines.append(f"Last observed: {snapshot['observed_at_local']} ({snapshot.get('age_seconds')} seconds ago)")
    if snapshot.get("source", {}).get("path"):
        lines.append(f"Source: {snapshot['source']['path']}")
    if snapshot.get("error"):
        lines.extend(["", f"Error: {snapshot['error']}"])
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "",
            "| Window | Used | Remaining | Reset time | Seconds until reset |",
            "| --- | ---: | ---: | --- | ---: |",
        ]
    )
    for window in snapshot.get("windows", {}).values():
        lines.append(
            "| {name} | {used} | {remaining} | {reset} | {seconds} |".format(
                name=window.get("name", "unknown"),
                used=_percent(window.get("last_observed_used_percent")),
                remaining=_percent(window.get("last_observed_remaining_percent")),
                reset=window.get("reset_at_local") or "unknown",
                seconds=window.get("seconds_until_reset")
                if window.get("seconds_until_reset") is not None
                else "unknown",
            )
        )

    resets = snapshot.get("available_resets", {})
    reset_value = resets.get("value")
    lines.extend(
        [
            "",
            f"Available reset count: {reset_value if reset_value is not None else 'not exposed in latest event'}",
        ]
    )
    warnings = snapshot.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the latest local Codex rate-limit snapshot.")
    parser.add_argument("--codex-home", help="Path to CODEX_HOME, for example C:\\Users\\you\\.codex or /mnt/c/Users/you/.codex.")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--stale-after-minutes", type=int, default=int(os.environ.get("CODEX_USAGE_STALE_AFTER_MINUTES", DEFAULT_STALE_AFTER_MINUTES)))
    parser.add_argument("--refresh", action="store_true", help="Run a minimal codex exec request before reading the rate-limit snapshot.")
    parser.add_argument("--codex-command", default=os.environ.get("CODEX_USAGE_CODEX_COMMAND", "codex"), help="Codex CLI command to run for --refresh.")
    parser.add_argument("--refresh-prompt", default=os.environ.get("CODEX_USAGE_REFRESH_PROMPT", DEFAULT_REFRESH_PROMPT))
    parser.add_argument("--refresh-timeout-seconds", type=int, default=int(os.environ.get("CODEX_USAGE_REFRESH_TIMEOUT_SECONDS", DEFAULT_REFRESH_TIMEOUT_SECONDS)))
    parser.add_argument("--refresh-cwd", default=os.environ.get("CODEX_USAGE_REFRESH_CWD"), help="Optional working directory for codex exec.")
    args = parser.parse_args(argv)

    refresh_result = None
    if args.refresh:
        refresh_result = run_codex_refresh(
            codex_command=args.codex_command,
            codex_home=args.codex_home,
            prompt=args.refresh_prompt,
            timeout_seconds=args.refresh_timeout_seconds,
            cwd=args.refresh_cwd,
        )

    snapshot = build_snapshot(args.codex_home, stale_after_minutes=args.stale_after_minutes)
    if refresh_result:
        snapshot["refresh"] = refresh_result
        if refresh_result.get("status") not in {"ok"}:
            snapshot["snapshot_type"] = "old_local_log_snapshot"
            snapshot.setdefault("warnings", []).append(
                "Codex CLI refresh failed or did not produce a fresh rate-limit event; returning the old local log snapshot."
            )
        elif snapshot.get("status") == "stale":
            snapshot["snapshot_type"] = "stale_local_log_snapshot"
        else:
            snapshot["snapshot_type"] = "refreshed_local_log_snapshot"
    elif snapshot.get("status") == "stale":
        snapshot["snapshot_type"] = "old_local_log_snapshot"
    if args.format == "markdown":
        sys.stdout.write(format_markdown(snapshot))
    else:
        sys.stdout.write(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
        sys.stdout.write("\n")
    return 0 if snapshot.get("status") in {"ok", "stale"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
