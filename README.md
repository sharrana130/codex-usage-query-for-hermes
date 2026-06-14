# Codex Usage Query for Hermes

A Hermes plugin that refreshes Codex CLI, reads local Codex rate-limit logs, and shows 5-hour and weekly usage remaining.

## What It Does

When you ask Hermes for Codex remaining usage, this plugin:

1. Runs a lightweight Codex CLI refresh:

   ```bash
   codex exec --json --sandbox read-only --skip-git-repo-check "只回复 OK，不修改文件。"
   ```

2. Reads the newest local `codex.rate_limits` event from Codex log databases.

3. Returns the 5-hour and weekly usage windows, remaining percentages, reset times, and snapshot status.

The plugin does not read or print `~/.codex/auth.json`, browser cookies, or access tokens.

## What It Can Show

- 5-hour window: usually `primary`, `window_minutes = 300`
- Weekly window: usually `secondary`, `window_minutes = 10080`
- Plan type
- Whether Codex is currently allowed or limit-reached
- Last observed snapshot time
- Reset time for each window
- Whether the snapshot is stale
- Available reset count if Codex exposes it in the event

If Codex does not expose a reset-count field, the plugin reports:

```text
not exposed in latest event
```

## Shortcut Commands

These shortcuts are declared in `hermes.plugin.json`:

```text
/codex用量
/codex-usage
```

Both call `get_codex_usage` with:

```json
{
  "refresh": true,
  "output_format": "markdown"
}
```

## Refresh Fallback

If Codex CLI is unavailable, times out, is not logged in, or does not write a fresh rate-limit event, the plugin returns the cached old local log snapshot instead of failing.

The response is clearly labeled:

```text
Snapshot type: old local log snapshot
```

The JSON field is:

```json
{
  "snapshot_type": "old_local_log_snapshot"
}
```

## Direct CLI Usage

From the plugin directory:

```bash
python3 scripts/codex_usage_meter.py --refresh --format markdown
```

For WSL reading Windows Codex Desktop state:

```bash
python3 scripts/codex_usage_meter.py \
  --refresh \
  --codex-home "/mnt/c/Users/<YourUser>/.codex" \
  --format markdown
```

If Hermes uses a non-default Codex CLI path:

```bash
python3 scripts/codex_usage_meter.py \
  --refresh \
  --codex-command "/path/to/codex" \
  --codex-home "/mnt/c/Users/<YourUser>/.codex" \
  --format markdown
```

Read the cached snapshot without refreshing:

```bash
python3 scripts/codex_usage_meter.py --format markdown
```

## MCP Setup

If Hermes supports MCP stdio servers, configure:

```json
{
  "mcpServers": {
    "codex-usage-meter": {
      "command": "python3",
      "args": [
        "/path/to/codex-usage-query-for-hermes/scripts/mcp_server.py",
        "--codex-home",
        "/mnt/c/Users/<YourUser>/.codex"
      ]
    }
  }
}
```

Tool name:

```text
get_codex_usage
```

Useful tool arguments:

- `refresh`: default `true`
- `codex_home`: Codex home path, for example `/mnt/c/Users/<YourUser>/.codex`
- `codex_command`: Codex CLI executable, default `codex`
- `refresh_timeout_seconds`: default `180`
- `output_format`: `markdown` or `json`
- `stale_after_minutes`: default `30`

## Files

- `.codex-plugin/plugin.json`: Codex plugin manifest
- `hermes.plugin.json`: Hermes plugin manifest
- `.mcp.json`: MCP server config
- `scripts/codex_usage_meter.py`: CLI probe
- `scripts/mcp_server.py`: MCP stdio server
- `skills/codex-usage-query-for-hermes/SKILL.md`: Codex/Hermes skill file
- `Codex Usage Query for Hermes skill.md`: shareable copy of the skill file

## Important Boundary

OpenAI public docs confirm that Codex can use ChatGPT subscription access and that enterprise Analytics APIs exist, but there is no public stable personal Pro quota API for real-time 5-hour, weekly, or reset-count lookup.

This plugin therefore implements a practical near-real-time workflow: Hermes triggers Codex CLI to refresh, then reads the local real rate-limit event Codex wrote. This is close to real-time, but it is not a zero-cost official quota API.
