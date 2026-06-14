import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from codex_usage_meter import build_snapshot, format_markdown, run_codex_refresh  # noqa: E402


class CodexUsageMeterTests(unittest.TestCase):
    def _make_db(self, root: Path, event: dict, ts: int | None = None) -> None:
        db = root / "logs_2.sqlite"
        con = sqlite3.connect(db)
        con.execute("create table logs (id integer primary key autoincrement, ts integer not null, feedback_log_body text)")
        if ts is None:
            ts = int(time.time() * 1000)
        con.execute(
            "insert into logs (ts, feedback_log_body) values (?, ?)",
            (ts, "websocket event: " + json.dumps(event, separators=(",", ":"))),
        )
        con.commit()
        con.close()

    def test_reads_latest_rate_limit_event(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            self._make_db(
                root,
                {
                    "type": "codex.rate_limits",
                    "plan_type": "prolite",
                    "rate_limits": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary": {
                            "used_percent": 25,
                            "window_minutes": 300,
                            "reset_after_seconds": 3600,
                            "reset_at": now + 3600,
                        },
                        "secondary": {
                            "used_percent": 40,
                            "window_minutes": 10080,
                            "reset_after_seconds": 86400,
                            "reset_at": now + 86400,
                        },
                    },
                    "code_review_rate_limits": None,
                },
            )

            snapshot = build_snapshot(root, stale_after_minutes=60)
            self.assertEqual(snapshot["status"], "ok")
            self.assertEqual(snapshot["plan_type"], "prolite")
            self.assertEqual(snapshot["windows"]["5_hour"]["last_observed_remaining_percent"], 75.0)
            self.assertEqual(snapshot["windows"]["weekly"]["last_observed_remaining_percent"], 60.0)
            self.assertEqual(snapshot["available_resets"]["availability"], "not_exposed_in_latest_event")

    def test_unavailable_when_no_event_exists(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "logs_2.sqlite")
            con.execute("create table logs (id integer primary key autoincrement, ts integer not null, feedback_log_body text)")
            con.commit()
            con.close()

            snapshot = build_snapshot(root)
            self.assertEqual(snapshot["status"], "unavailable")
            self.assertIn("No codex.rate_limits event", snapshot["error"])

    def test_markdown_contains_windows(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            self._make_db(
                root,
                {
                    "type": "codex.rate_limits",
                    "rate_limits": {
                        "primary": {"used_percent": 1, "window_minutes": 300, "reset_at": now + 1},
                        "secondary": {"used_percent": 2, "window_minutes": 10080, "reset_at": now + 2},
                    },
                },
            )
            text = format_markdown(build_snapshot(root))
            self.assertIn("5_hour", text)
            self.assertIn("weekly", text)

    def test_refresh_reports_missing_codex_command(self):
        result = run_codex_refresh(codex_command="definitely-not-a-real-codex-command", timeout_seconds=1)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["new_rate_limit_event_observed"])

    def test_markdown_marks_old_snapshot_when_refresh_failed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            self._make_db(
                root,
                {
                    "type": "codex.rate_limits",
                    "rate_limits": {
                        "primary": {"used_percent": 1, "window_minutes": 300, "reset_at": now + 1},
                        "secondary": {"used_percent": 2, "window_minutes": 10080, "reset_at": now + 2},
                    },
                },
            )
            snapshot = build_snapshot(root)
            snapshot["refresh"] = {"requested": True, "status": "failed"}
            snapshot["snapshot_type"] = "old_local_log_snapshot"
            text = format_markdown(snapshot)
            self.assertIn("Refresh: failed", text)
            self.assertIn("Snapshot type: old local log snapshot", text)

    def test_hermes_manifest_has_slash_shortcuts(self):
        manifest = json.loads((ROOT / "hermes.plugin.json").read_text(encoding="utf-8"))
        commands = {item["name"] for item in manifest["commands"]}
        shortcuts = {item["trigger"] for item in manifest["shortcuts"]}
        slash_commands = {item["command"] for item in manifest["slash_commands"]}
        self.assertIn("/codex用量", commands)
        self.assertIn("/codex-usage", commands)
        self.assertIn("/codex用量", shortcuts)
        self.assertIn("/codex-usage", slash_commands)


if __name__ == "__main__":
    unittest.main()
