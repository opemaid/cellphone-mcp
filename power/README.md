# cellphone-power

A companion MCP server to cellphone-mcp. It gives an AI agent **everything the adb `shell` user can reach on an Android phone, with no root needed**. cellphone-mcp handles the screen (taps, swipes, screenshots). This server handles the data and the system.

| Area | Tools |
|---|---|
| System | `phone_shell`, `phone_info`, `phone_logcat`, `phone_settings` |
| Notifications | `phone_notifications`: full, unredacted title and text of every active notification |
| Personal data (read-only) | `phone_sms`, `phone_contacts`, `phone_call_log` |
| Files & media | `phone_files`, `phone_recent_media`, `phone_pull`, `phone_push`, `phone_screen_record` |
| Apps | `phone_apps`, `phone_app_info`, `phone_app_permission`, `phone_appop` (e.g. turn an app's notifications back on), `phone_open` (URL or app, using an intent, no taps) |
| Chrome (DevTools) | `chrome_tabs`, `chrome_open_tab`, `chrome_activate_tab`, `chrome_page_text`, `chrome_links`, `chrome_click`, `chrome_fill`, `chrome_scroll`, `chrome_eval` |

The Chrome tools talk to Chrome's DevTools socket (`chrome_devtools_remote`), which Chrome opens whenever USB debugging is on. This lets an agent read the real page text and click links by their text, instead of guessing from screenshots. Chrome freezes background tabs, so the tools bring a tab to the front before reading it.

## What it can't do (no root)

- Read other apps' private storage (`/data/data/...`), e.g. WhatsApp or Messenger message databases. Use `phone_notifications` for incoming messages instead.
- Unlock the phone, or see FLAG_SECURE screens such as banking apps.
- Anything that needs a system-signature permission.

## Safety

- `phone_shell` refuses commands that delete data, wipe, reboot, uninstall, clear app data or change lock/secure settings unless `confirm=true` is passed. Agents are told to ask the user first.
- `phone_settings` refuses writes to `secure` settings.
- `chrome_click` and `chrome_fill` can post or send things on sites where you are logged in. `chrome_fill` never submits a form by itself.
- SMS (including one-time codes), contacts and call logs are sensitive. Only connect this server to agents you trust.

## Install

Requires: Android platform-tools (`adb`), [uv](https://docs.astral.sh/uv/), a phone with USB debugging on and this PC authorized.

Claude Code:

```
claude mcp add cellphone-power -s user -- uv run --directory /path/to/cellphone-mcp/power --with-requirements requirements.txt python server.py
```

Environment variables, all optional:

- `CELLPHONE_SERIAL`: the device to use if more than one is connected
- `ANDROID_HOME`: where to find platform-tools
- `CELLPHONE_CDP_PORT`: local port for the Chrome DevTools forward (default 9333)

## Device tests

`power/tests/` contains end-to-end suites that run every tool of both servers against a real phone through an MCP client. Every change they make is reverted: settings are restored, test files deleted, test tabs closed and the test app uninstalled.

```
set CELLPHONE_SERIAL=<serial from adb devices>
uv run --with "mcp>=1.2,<2" python power/tests/test_power_device.py
uv run --with "mcp>=1.2,<2" python power/tests/test_mobile_device.py
```

They were written against a Redmi Note 14 Pro on HyperOS 3, and the cellphone-mcp suite uses the MIUI Calculator. On another phone, adjust the app-specific parts.
