#!/usr/bin/env python3
"""Tiny stdio MCP server for Codex Usage Meter."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from codex_usage_meter import (
    DEFAULT_REFRESH_PROMPT,
    DEFAULT_REFRESH_TIMEOUT_SECONDS,
    VERSION,
    build_snapshot,
    format_markdown,
    run_codex_refresh,
)


SERVER_NAME = "codex-usage-meter"


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.lower()] = value.strip()

    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        return None
    if length <= 0:
        return None
    payload = sys.stdin.buffer.read(length)
    return json.loads(payload.decode("utf-8"))


def _send_message(message: dict[str, Any]) -> None:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _tool_schema() -> dict[str, Any]:
    return {
        "name": "get_codex_usage",
        "title": "Get Codex Usage",
        "description": (
            "Read the latest locally cached Codex rate-limit event and return "
            "5-hour and weekly usage windows. This is read-only and does not "
            "read Codex auth tokens. Shortcut aliases: /codex用量 and /codex-usage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "codex_home": {
                    "type": "string",
                    "description": "Optional CODEX_HOME path. Use /mnt/c/Users/<user>/.codex from WSL.",
                },
                "stale_after_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 30,
                    "description": "Mark the snapshot stale after this many minutes.",
                },
                "output_format": {
                    "type": "string",
                    "enum": ["json", "markdown"],
                    "default": "markdown",
                },
                "refresh": {
                    "type": "boolean",
                    "default": True,
                    "description": "Run a minimal codex exec request first so the returned rate-limit snapshot is fresh.",
                },
                "codex_command": {
                    "type": "string",
                    "default": "codex",
                    "description": "Codex CLI executable to use for refresh.",
                },
                "refresh_timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "default": DEFAULT_REFRESH_TIMEOUT_SECONDS,
                },
                "refresh_prompt": {
                    "type": "string",
                    "default": DEFAULT_REFRESH_PROMPT,
                },
                "refresh_cwd": {
                    "type": "string",
                    "description": "Optional working directory for codex exec.",
                },
            },
            "additionalProperties": False,
        },
    }


def _handle_request(request: dict[str, Any], default_codex_home: str | None) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    if request_id is None and method and method.startswith("notifications/"):
        return None

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [_tool_schema()]}}
        if method == "tools/call":
            name = params.get("name")
            if name != "get_codex_usage":
                raise ValueError(f"Unknown tool: {name}")
            args = params.get("arguments") or {}
            codex_home = args.get("codex_home") or default_codex_home
            stale_after = int(args.get("stale_after_minutes") or 30)
            output_format = args.get("output_format") or "markdown"
            refresh_requested = bool(args.get("refresh", True))
            refresh_result = None
            if refresh_requested:
                refresh_result = run_codex_refresh(
                    codex_command=args.get("codex_command") or "codex",
                    codex_home=codex_home,
                    prompt=args.get("refresh_prompt") or DEFAULT_REFRESH_PROMPT,
                    timeout_seconds=int(args.get("refresh_timeout_seconds") or DEFAULT_REFRESH_TIMEOUT_SECONDS),
                    cwd=args.get("refresh_cwd"),
                )
            snapshot = build_snapshot(codex_home, stale_after_minutes=stale_after)
            if refresh_result:
                snapshot["refresh"] = refresh_result
                if refresh_result.get("status") != "ok":
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
            text = format_markdown(snapshot) if output_format == "markdown" else json.dumps(snapshot, ensure_ascii=False, indent=2)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": snapshot,
                    "isError": snapshot.get("status") == "unavailable",
                },
            }
        raise ValueError(f"Unsupported method: {method}")
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": str(exc)},
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Codex Usage Meter as an MCP stdio server.")
    parser.add_argument("--codex-home", help="Default CODEX_HOME path to inspect.")
    args = parser.parse_args(argv)

    while True:
        request = _read_message()
        if request is None:
            break
        response = _handle_request(request, args.codex_home)
        if response is not None:
            _send_message(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
