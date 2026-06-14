---
name: codex-usage-query-for-hermes
description: Query Codex remaining usage for Hermes by refreshing Codex CLI and reading local rate-limit snapshots.
---

# Codex Usage Query for Hermes

Use this skill when the user asks Hermes to show Codex remaining usage, including the 5-hour and weekly rate-limit windows.

Shortcut commands:

- `/codex用量`
- `/codex-usage`

Run the refresh-plus-read probe:

```bash
python3 scripts/codex_usage_meter.py --refresh --format markdown
```

If running from WSL against Windows Codex Desktop state, pass the Windows Codex home:

```bash
python3 scripts/codex_usage_meter.py --refresh --codex-home /mnt/c/Users/<WindowsUser>/.codex --format markdown
```

The refresh step runs:

```bash
codex exec --json --sandbox read-only --skip-git-repo-check "只回复 OK，不修改文件。"
```

Then it reads `logs_*.sqlite`.

If Codex CLI is unavailable, timed out, not logged in, or does not write a fresh rate-limit event, return the cached old local log snapshot and clearly label it:

```text
Snapshot type: old local log snapshot
```

Do not read, print, or copy `auth.json`.
