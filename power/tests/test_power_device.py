"""End-to-end test of every cellphone-power tool through a real MCP stdio client.
Prints only shapes/counts (never personal data). Every change is reverted."""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

POWER = str(Path(__file__).resolve().parents[1])
APP = "com.miui.compass"
results = []


def shape(v):
    if isinstance(v, list):
        return f"list[{len(v)}]" + (f" keys={sorted(v[0])}" if v and isinstance(v[0], dict) else "")
    if isinstance(v, dict):
        return f"dict keys={list(v)[:8]}"
    s = str(v).replace("\n", " ")
    return repr(s[:90])


async def main():
    params = StdioServerParameters(command="uv", args=["run", "--quiet", "--directory", POWER,
                                   "--with-requirements", "requirements.txt", "python", "server.py"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}

            async def call(name, args=None, show=True, expect=None):
                t0 = time.time()
                try:
                    res = await s.call_tool(name, args or {})
                    if res.isError:
                        raise RuntimeError(res.content[0].text[:200])
                    v = res.structuredContent.get("result") if res.structuredContent else None
                    if v is None:
                        txt = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                        try:
                            v = json.loads(txt)
                        except Exception:
                            v = txt
                    ok = True if expect is None else expect(v)
                    results.append((name, "PASS" if ok else "FAIL", f"{time.time()-t0:.1f}s", shape(v) if show else "(hidden)"))
                    return v
                except Exception as e:
                    results.append((name, "ERROR", f"{time.time()-t0:.1f}s", str(e)[:150]))
                    return None

            # --- system
            await call("phone_info", expect=lambda v: bool(v.get("model")))
            await call("phone_shell", {"command": "getprop ro.product.brand"}, expect=lambda v: "Redmi" in v)
            await call("phone_shell", {"command": "pm clear com.example.nothing"}, expect=lambda v: v.startswith("REFUSED"))
            await call("phone_logcat", {"lines": 100}, expect=lambda v: len(v) > 0)
            await call("phone_logcat", {"lines": 50, "since_seconds": 120}, expect=lambda v: isinstance(v, str))
            await call("phone_logcat", {"lines": 300, "filter": "ActivityManager|ActivityTaskManager"}, expect=lambda v: isinstance(v, str))

            # --- settings: read, write+restore, secure refusal
            orig = (await call("phone_settings", {"namespace": "system", "key": "screen_off_timeout"})).strip()
            await call("phone_settings", {"namespace": "system", "key": "screen_off_timeout", "value": "300000"},
                       expect=lambda v: v.endswith("300000"))
            await call("phone_settings", {"namespace": "system", "key": "screen_off_timeout", "value": orig},
                       expect=lambda v: v.endswith(orig))
            await call("phone_settings", {"namespace": "secure", "key": "x_test", "value": "1"},
                       expect=lambda v: v.startswith("REFUSED"))
            await call("phone_settings", {"namespace": "global"}, show=False, expect=lambda v: "=" in v)

            # --- notifications
            n = await call("phone_notifications", show=False, expect=lambda v: isinstance(v, list))
            if n:
                pkg = n[0]["package"]
                await call("phone_notifications", {"package": pkg}, show=False, expect=lambda v: all(x["package"] == pkg for x in v))
            await call("phone_notifications", {"include_system": True}, show=False, expect=lambda v: len(v) >= len(n or []))

            # --- personal data (hidden output)
            await call("phone_sms", {"limit": 3}, show=False, expect=lambda v: len(v) == 3 and "body" in v[0])
            await call("phone_sms", {"box": "sent", "limit": 2}, show=False, expect=lambda v: isinstance(v, list))
            await call("phone_sms", {"box": "all", "contains": "the", "limit": 2}, show=False, expect=lambda v: isinstance(v, list))
            await call("phone_contacts", {"limit": 3}, show=False, expect=lambda v: len(v) > 0 and "numbers" in v[0])
            await call("phone_contacts", {"name_contains": "Bal"}, show=False, expect=lambda v: isinstance(v, list))
            await call("phone_call_log", {"limit": 3}, show=False, expect=lambda v: len(v) > 0 and "type" in v[0])

            # --- files: push, list, pull, compare, delete (needs confirm), verify gone
            local = os.path.join(tempfile.gettempdir(), "cellphone_power_test.txt")
            back = local + ".back"
            payload = f"cellphone-power test {time.time()}"
            open(local, "w").write(payload)
            remote = "/sdcard/Download/cellphone_power_test.txt"
            await call("phone_push", {"local_path": local, "remote_path": remote}, expect=lambda v: "1 file pushed" in v)
            await call("phone_files", {"path": "/sdcard/Download"}, show=False, expect=lambda v: "cellphone_power_test.txt" in v)
            await call("phone_pull", {"remote_path": remote, "local_path": back}, expect=lambda v: "1 file pulled" in v)
            results.append(("pull roundtrip", "PASS" if open(back).read() == payload else "FAIL", "", "content matches"))
            await call("phone_shell", {"command": f"rm -f {remote}"}, expect=lambda v: v.startswith("REFUSED"))
            await call("phone_shell", {"command": f"rm -f {remote}", "confirm": True}, expect=lambda v: v.strip() == "")
            await call("phone_files", {"path": "/sdcard/Download"}, show=False, expect=lambda v: "cellphone_power_test.txt" not in v)
            os.remove(local); os.remove(back)
            for kind in ("images", "video", "audio", "downloads"):
                await call("phone_recent_media", {"kind": kind, "limit": 2}, show=False, expect=lambda v: isinstance(v, list))

            # --- screen record 3s, check file, delete it (it shows the private screen)
            rec = os.path.join(tempfile.gettempdir(), "cellphone_power_rec.mp4")
            p = await call("phone_screen_record", {"seconds": 3, "local_path": rec})
            size = os.path.getsize(rec) if p and os.path.exists(rec) else 0
            results.append(("screen_record file", "PASS" if size > 10000 else "FAIL", "", f"{size} bytes"))
            if os.path.exists(rec):
                os.remove(rec)
            await call("phone_shell", {"command": "ls /sdcard/cellphone_rec_* 2>/dev/null | wc -l"},
                       expect=lambda v: v.strip() == "0")

            # --- apps: info, permission revoke+restore, appop set+restore (on Compass)
            await call("phone_apps", expect=lambda v: APP in v)
            await call("phone_apps", {"third_party_only": False, "contains": "chrome"}, expect=lambda v: "com.android.chrome" in v)
            info = await call("phone_app_info", {"package": APP}, expect=lambda v: v.get("version"))
            perm = "android.permission.ACCESS_FINE_LOCATION"
            was = perm in (info or {}).get("granted", [])
            await call("phone_app_permission", {"package": APP, "permission": perm, "grant": not was},
                       expect=lambda v: ("is now GRANTED" in v) == (not was))
            await call("phone_app_permission", {"package": APP, "permission": perm, "grant": was},
                       expect=lambda v: ("is now GRANTED" in v) == was)
            op0 = await call("phone_shell", {"command": f"cmd appops get {APP} VIBRATE"})
            await call("phone_appop", {"package": APP, "op": "VIBRATE", "mode": "ignore"}, expect=lambda v: "ignore" in v)
            await call("phone_appop", {"package": APP, "op": "VIBRATE", "mode": "default"}, expect=lambda v: "VIBRATE" in v)
            await call("phone_appop", {"package": APP, "op": "VIBRATE", "mode": "bogus"}, expect=lambda v: v.startswith("mode must"))
            results.append(("appop restored", "INFO", "", f"before={(op0 or '').strip()!r}"))

            # --- intents
            await call("phone_open", {"package": APP}, expect=lambda v: "Events injected" in v or "Starting" in v)
            time.sleep(2)
            await call("phone_shell", {"command": "dumpsys activity activities | grep -m1 topResumedActivity"},
                       expect=lambda v: APP in v)
            await call("phone_open", {"url": "https://example.com/?cellphone_power_test=1", "package": "com.android.chrome"},
                       expect=lambda v: "Starting" in v or "Warning" in v)
            time.sleep(4)

            # --- chrome
            await call("chrome_tabs", expect=lambda v: any("example.com" in t["url"] for t in v))
            await call("chrome_page_text", {"tab": "cellphone_power_test", "max_chars": 300},
                       expect=lambda v: "Example Domain" in v)
            await call("chrome_links", {"tab": "cellphone_power_test"}, expect=lambda v: len(v) >= 1)
            await call("chrome_eval", {"javascript": "document.body.insertAdjacentHTML('beforeend', '<input id=cpt>'); 1+1",
                                       "tab": "cellphone_power_test"}, expect=lambda v: v.strip() == "2")
            await call("chrome_fill", {"selector": "#cpt", "value": "hello phone", "tab": "cellphone_power_test"},
                       expect=lambda v: v == "filled")
            await call("chrome_eval", {"javascript": "document.querySelector('#cpt').value", "tab": "cellphone_power_test"},
                       expect=lambda v: "hello phone" in v)
            await call("chrome_scroll", {"tab": "cellphone_power_test", "pixels": 100}, expect=lambda v: "/" in v)
            await call("chrome_click", {"text": "zzz-no-such-element", "tab": "cellphone_power_test"},
                       expect=lambda v: v == "NOT FOUND")
            await call("chrome_click", {"text": "Learn more", "tab": "cellphone_power_test"},
                       expect=lambda v: v.startswith("clicked"))
            time.sleep(3)
            await call("chrome_tabs", expect=lambda v: any("iana.org" in t["url"] for t in v))
            await call("chrome_open_tab", {"url": "https://example.org/?cellphone_power_tab=1"}, expect=lambda v: v.get("id"))
            time.sleep(3)
            await call("chrome_activate_tab", {"tab": "cellphone_power_tab"}, expect=lambda v: v.startswith("activated"))
            await call("chrome_close_tab", {"tab": "cellphone_power_tab"}, expect=lambda v: v.startswith("closed"))
            await call("chrome_close_tab", {"tab": "iana.org"}, expect=lambda v: v.startswith("closed"))
            await call("chrome_tabs", expect=lambda v: not any(("iana.org" in t["url"]) or ("cellphone_power" in t["url"]) for t in v))

            tested = {r[0] for r in results}
            results.append(("COVERAGE", "PASS" if tools <= tested else "FAIL", "", f"untested={sorted(tools - tested)}"))


asyncio.run(main())
w = max(len(r[0]) for r in results)
for r in results:
    print(f"{r[1]:5} {r[0]:<{w}} {r[2]:>6}  {r[3]}")
bad = [r for r in results if r[1] in ("FAIL", "ERROR")]
print(f"\n{len(results)} checks, {len(bad)} failed")
sys.exit(1 if bad else 0)
