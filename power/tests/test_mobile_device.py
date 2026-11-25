"""End-to-end test of the cellphone-mcp (Node) tools on the real phone, via an MCP stdio client.
Works inside the Calculator app and a scratch example.com tab. Everything is reverted.
cellphone-power is used as the independent checker (Chrome DOM, settings, files)."""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

D = os.environ["CELLPHONE_SERIAL"]
ROOT = Path(__file__).resolve().parents[2]
CALC = "com.miui.calculator"
results = []
called = set()


def shape(v):
    s = str(v).replace("\n", " ")
    return repr(s[:95])


async def main():
    node = StdioServerParameters(command="node", args=[str(ROOT / "lib" / "index.js")])
    power = StdioServerParameters(command="uv", args=["run", "--quiet", "--directory",
                                  str(ROOT / "power"), "--with-requirements",
                                  "requirements.txt", "python", "server.py"])
    async with stdio_client(node) as (r1, w1), stdio_client(power) as (r2, w2):
        async with ClientSession(r1, w1) as m, ClientSession(r2, w2) as p:
            await m.initialize(); await p.initialize()
            tools = {t.name for t in (await m.list_tools()).tools}

            async def call(sess, name, args=None, expect=None, label=None, show=True, allow_error=False):
                args = dict(args or {})
                if sess is m and name not in ("mobile_list_available_devices",) and "device" not in args:
                    args["device"] = D
                t0 = time.time()
                try:
                    res = await sess.call_tool(name, args)
                    parts = []
                    for c in res.content:
                        parts.append(c.text if getattr(c, "type", "") == "text" else f"<{c.type} {getattr(c,'mimeType','')}>")
                    v = "\n".join(parts)
                    if res.isError and not allow_error:
                        raise RuntimeError(v[:200])
                    ok = True if expect is None else bool(expect(v))
                    status = ("PASS" if ok else "FAIL") if not (res.isError and allow_error) else ("N/A" if ok else "FAIL")
                    called.add(name)
                    results.append((label or name, status, f"{time.time()-t0:.1f}s", shape(v) if show else "(hidden)"))
                    return v
                except Exception as e:
                    results.append((label or name, "ERROR", f"{time.time()-t0:.1f}s", str(e)[:160]))
                    return None

            async def pw(name, args=None):
                res = await p.call_tool(name, args or {})
                return "\n".join(c.text for c in res.content if hasattr(c, "text"))

            # ---------- discovery / read-only
            await call(m, "mobile_list_available_devices", expect=lambda v: D in v)
            await call(m, "mobile_get_screen_size", expect=lambda v: "Screen size is" in v)
            await call(m, "mobile_list_apps", show=False, expect=lambda v: CALC in v)
            await call(m, "mobile_get_orientation", expect=lambda v: "portrait" in v.lower())
            await call(m, "mobile_get_device_logs", {"limit": 5}, show=False, expect=lambda v: len(v) > 0)
            crashes = await call(m, "mobile_list_crashes", show=False, expect=lambda v: v is not None)
            ids = re.findall(r'"id"\s*:\s*"([^"]+)"', crashes or "")
            if ids:
                await call(m, "mobile_get_crash", {"id": ids[0]}, show=False, expect=lambda v: len(v) > 0)
            else:
                results.append(("mobile_get_crash", "N/A", "", "no crashes on device to fetch"))
            await call(m, "mobile_take_screenshot", {"maxSize": 400}, show=False, expect=lambda v: "<image" in v)
            shot = os.path.join(tempfile.gettempdir(), "cpm_shot.png")
            await call(m, "mobile_save_screenshot", {"saveTo": shot, "maxSize": 400}, expect=lambda v: os.path.exists(shot))
            os.path.exists(shot) and os.remove(shot)

            await call(m, "mobile_press_button", {"button": "HOME"}, label="press HOME (start clean)")
            # ---------- Calculator: launch, element refs, click, double tap, long press, drag
            await call(m, "mobile_launch_app", {"packageName": CALC}, expect=lambda v: "Launched" in v)
            time.sleep(2)
            await call(m, "mobile_get_foreground_app", expect=lambda v: CALC in v)
            els = await call(m, "mobile_list_elements_on_screen", show=False, expect=lambda v: "@e" in v)
            await call(m, "mobile_list_elements_on_screen", {"format": "json"}, show=False, label="elements (json)",
                       expect=lambda v: "[{" in v and "\"ref\"" in v)
            # clear, then type 7 via ref, 8 via coordinates
            def ref_for(label):
                for line in (els or "").splitlines():
                    if re.search(rf'(text|label)="{re.escape(label)}"', line):
                        return line.split()[0]
            def center(ref):
                for line in (els or "").splitlines():
                    if line.startswith(ref + " "):
                        x, y = map(int, re.search(r"at=(\d+),(\d+)", line).groups())
                        w, h = map(int, re.search(r"size=(\d+)x(\d+)", line).groups())
                        return x + w // 2, y + h // 2
            clr = ref_for("C") or ref_for("AC") or ref_for("clear")
            seven, eight = ref_for("7"), ref_for("8")
            results.append(("calc refs found", "PASS" if seven and eight else "FAIL", "", f"clear={clr} 7={seven} 8={eight}"))
            if clr:
                await call(m, "mobile_click_on_screen_at_coordinates", {"ref": clr}, label="click (ref: clear)")
            await call(m, "mobile_click_on_screen_at_coordinates", {"ref": seven}, label="click (ref: 7)")
            x8, y8 = center(eight)
            await call(m, "mobile_click_on_screen_at_coordinates", {"x": x8, "y": y8}, label="click (xy: 8)")
            await call(m, "mobile_double_tap_on_screen", {"ref": seven}, label="double tap (7 7)")
            await call(m, "mobile_long_press_on_screen_at_coordinates", {"ref": eight, "duration": 800}, label="long press (8)")
            sw, sh = 540, 1200
            await call(m, "mobile_drag_on_screen", {"x1": 300, "y1": 600, "x2": 700, "y2": 600}, label="drag")
            await call(m, "mobile_swipe_on_screen", {"direction": "left", "x": 900, "y": 700, "distance": 300}, label="swipe left")
            await call(m, "mobile_batch_commands", {"steps": [
                {"name": "mobile_click_on_screen_at_coordinates", "arguments": {"ref": clr}} if clr else
                {"name": "mobile_press_button", "arguments": {"button": "BACK"}},
                {"name": "delay", "arguments": {"milliseconds": 300}},
                {"name": "mobile_get_screen_size", "arguments": {}}],
                "listElementsAtEnd": True}, show=False, label="batch (clear+delay+size)", expect=lambda v: "1080" in v)

            await call(m, "mobile_terminate_app", {"packageName": CALC}, expect=lambda v: "erminat" in v or "stop" in v.lower())

            # ---------- clipboard (overwrites the clipboard with a test string)
            await call(m, "mobile_clipboard", {"text": "cellphone-mcp clipboard test"}, label="clipboard set")
            await call(m, "mobile_clipboard", {}, label="clipboard read", expect=lambda v: "cellphone-mcp clipboard test" in v)

            # ---------- Chrome scratch page: orientation + typing, verified through the DOM (cellphone-power)
            await pw("phone_open", {"url": "https://example.com/?cpm_type_test=1", "package": "com.android.chrome"})
            time.sleep(4)
            acc = (await pw("phone_settings", {"namespace": "system", "key": "accelerometer_rotation"})).strip()
            usr = (await pw("phone_settings", {"namespace": "system", "key": "user_rotation"})).strip()
            await call(m, "mobile_set_orientation", {"orientation": "landscape"})
            time.sleep(2)
            await call(m, "mobile_get_orientation", label="orientation after set", expect=lambda v: "landscape" in v.lower())
            await call(m, "mobile_get_screen_size", label="screen size in landscape", expect=lambda v: "Screen size is" in v)
            await call(m, "mobile_set_orientation", {"orientation": "portrait"}, label="orientation restore")
            await pw("phone_settings", {"namespace": "system", "key": "user_rotation", "value": usr})
            await pw("phone_settings", {"namespace": "system", "key": "accelerometer_rotation", "value": acc})
            acc2 = (await pw("phone_settings", {"namespace": "system", "key": "accelerometer_rotation"})).strip()
            results.append(("auto-rotate restored", "PASS" if acc2 == acc else "FAIL", "", f"before={acc} after={acc2}"))
            time.sleep(1)

            await pw("chrome_eval", {"tab": "cpm_type_test", "javascript":
                "document.body.insertAdjacentHTML('afterbegin', '<form id=f onsubmit=\"window.sub=(window.sub||0)+1;return false\">"
                "<input id=cpt style=\"font-size:40px;width:90%\"></form>'); window.sub=0; 1"})
            focus = {"tab": "cpm_type_test", "javascript": "document.querySelector('#cpt').focus(); 1"}
            await pw("chrome_eval", focus)
            await call(m, "mobile_type_keys", {"text": "hello phone", "submit": False}, label="type_keys")
            val = await pw("chrome_eval", {"tab": "cpm_type_test", "javascript": "document.querySelector('#cpt').value"})
            results.append(("typed value in DOM", "PASS" if "hello phone" in val else "FAIL", "", val[:60]))
            await pw("chrome_eval", focus)
            await call(m, "mobile_type_keys", {"text": "!", "submit": True}, label="type_keys submit=true")
            time.sleep(1)
            sub = await pw("chrome_eval", {"tab": "cpm_type_test", "javascript": "String(window.sub)"})
            results.append(("submit=true fires Enter", "PASS" if "1" in sub else "FAIL", "", f"form submitted={sub}"))
            await pw("chrome_eval", focus)
            await call(m, "mobile_press_button", {"button": "ENTER"}, label="press ENTER")
            time.sleep(1)
            sub2 = await pw("chrome_eval", {"tab": "cpm_type_test", "javascript": "String(window.sub)"})
            results.append(("ENTER button submits", "PASS" if "2" in sub2 else "FAIL", "", f"form submitted={sub2}"))
            await pw("chrome_close_tab", {"tab": "cpm_type_test"})

            # ---------- open_url (default browser)
            await call(m, "mobile_open_url", {"url": "https://example.com/?cpm_open_url=1"}, expect=lambda v: "pened" in v or "URL" in v)
            time.sleep(3)
            fg = await call(m, "mobile_get_foreground_app", label="foreground after open_url", expect=lambda v: "browser" in v.lower() or "chrome" in v.lower())
            if "chrome" in (fg or "").lower():
                try:
                    await pw("chrome_close_tab", {"tab": "cpm_open_url"})
                except Exception:
                    pass
            await call(m, "mobile_press_button", {"button": "HOME"}, label="press HOME")

            # ---------- screen recording start/stop -> file, then delete it
            rec = os.path.join(tempfile.gettempdir(), "cpm_rec.mp4")
            await call(m, "mobile_start_screen_recording", {"output": rec, "timeLimit": 10})
            time.sleep(3)
            await call(m, "mobile_stop_screen_recording", expect=lambda v: "rec" in v.lower() or ".mp4" in v)
            time.sleep(2)
            size = os.path.getsize(rec) if os.path.exists(rec) else 0
            results.append(("recording file", "PASS" if size > 10000 else "FAIL", "", f"{size} bytes"))
            os.path.exists(rec) and os.remove(rec)

            # ---------- not applicable to this phone (expect a clean error)
            await call(m, "mobile_fold_device", {"posture": "open"}, allow_error=True, expect=lambda v: True)
            await call(m, "mobile_set_location", {"latitude": -26.2, "longitude": 28.0}, allow_error=True, expect=lambda v: True)
            await call(m, "mobile_list_remote_devices", allow_error=True, expect=lambda v: True, show=True)

            skipped = {"mobile_install_app", "mobile_uninstall_app", "mobile_clear_app_data",
                       "mobile_login_to_cloud_provider", "mobile_allocate_remote_device", "mobile_release_remote_device"}
            tested = called
            missing = tools - tested - skipped
            results.append(("COVERAGE", "PASS" if not missing else "FAIL", "", f"untested={sorted(missing)} skipped-by-design={sorted(skipped)}"))


asyncio.run(main())
w = max(len(r[0]) for r in results)
for r in results:
    print(f"{r[1]:5} {r[0]:<{w}} {r[2]:>6}  {r[3]}")
bad = [r for r in results if r[1] in ("FAIL", "ERROR")]
print(f"\n{len(results)} checks, {len(bad)} failed")
