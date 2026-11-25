"""cellphone-power: no-root, adb-shell-level access to an Android phone, as an MCP server.

Complements cellphone-mcp (taps/swipes/screenshots). This server adds the data and system
surfaces that the adb `shell` user can reach WITHOUT root:

  notifications (full text), SMS, contacts, call log, shared-storage files and media,
  logcat, settings, app permissions / appops, screen recording, intents,
  and Chrome page DOM via the DevTools socket (read text, click, fill, run JS).

Hard limits (no root): other apps' private data (/data/data), the lock screen,
FLAG_SECURE screens, and anything needing system signature permissions.

Env: CELLPHONE_SERIAL (optional; default = the only connected device), ANDROID_HOME (optional).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cellphone-power")

# ---------------------------------------------------------------- adb plumbing


def _adb_path() -> str:
    exe = "adb.exe" if os.name == "nt" else "adb"
    home = os.environ.get("ANDROID_HOME")
    if home and (Path(home) / "platform-tools" / exe).exists():
        return str(Path(home) / "platform-tools" / exe)
    found = shutil.which("adb")
    if found:
        return found
    raise RuntimeError("adb not found: install Android platform-tools or set ANDROID_HOME")


def _serial() -> str:
    s = os.environ.get("CELLPHONE_SERIAL")
    if s:
        return s
    out = subprocess.run([_adb_path(), "devices"], capture_output=True, text=True, timeout=15).stdout
    devs = [l.split("\t")[0] for l in out.splitlines()[1:] if l.strip().endswith("\tdevice")]
    if not devs:
        raise RuntimeError("No authorized device. Plug in the phone, unlock it, accept the USB debugging prompt.")
    if len(devs) > 1:
        raise RuntimeError(f"Several devices connected {devs}; set CELLPHONE_SERIAL.")
    return devs[0]


def _adb(*args: str, timeout: int = 60, binary: bool = False):
    r = subprocess.run([_adb_path(), "-s", _serial(), *args], capture_output=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    if binary:
        return r.stdout
    out = r.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0 and not out:
        raise RuntimeError(err.strip() or f"adb exited {r.returncode}")
    return out


def _sh(cmd: str, timeout: int = 60) -> str:
    """Run one shell command string on the device (adb joins args into a single string anyway)."""
    return _adb("shell", cmd, timeout=timeout)


def _q(v: str) -> str:
    return shlex.quote(str(v))


def _clip(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + f"\n...[truncated {len(s) - limit} chars]"


def _ms(v: str | None) -> str | None:
    try:
        return datetime.fromtimestamp(int(v) / 1000).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return v


# ---------------------------------------------------------------- content providers


def _content_query(uri: str, projection: list[str], where: str | None = None,
                   sort: str | None = None) -> list[dict]:
    cmd = f"content query --uri {_q(uri)} --projection {_q(':'.join(projection))}"
    if where:
        cmd += f" --where {_q(where)}"
    if sort:
        cmd += f" --sort {_q(sort)}"
    out = _sh(cmd)
    if out.startswith("Error") or "usage:" in out[:200]:
        raise RuntimeError(out.strip()[:500])
    keys = "|".join(re.escape(k) for k in projection)
    rows = []
    for block in re.split(r"(?m)^Row: \d+ ", out)[1:]:
        block = block.rstrip("\n")
        parts = re.split(rf", (?=(?:{keys})=)", block)
        row = {}
        for p in parts:
            k, _, v = p.partition("=")
            row[k] = None if v == "NULL" else v
        rows.append(row)
    return rows


# ---------------------------------------------------------------- guard for mutating shell commands

_DANGEROUS = re.compile(
    r"\b(rm\s+-[a-z]*r|rm\s+-[a-z]*f|reboot|wipe|factory|recovery|pm\s+(uninstall|clear|disable)|"
    r"cmd\s+package\s+(uninstall|clear)|svc\s+(power|data|wifi)|settings\s+put\s+secure|"
    r"locksettings|dd\s+if=|mkfs|format)\b", re.I)


# ================================================================= tools: system


@mcp.tool()
def phone_shell(command: str, timeout_s: int = 60, confirm: bool = False) -> str:
    """Run any command as the adb `shell` user on the phone (no root) and return its output.
    Commands that delete, wipe, reboot, uninstall, clear app data or change lock/secure settings
    are refused unless confirm=true. Ask the user before passing confirm=true."""
    if _DANGEROUS.search(command) and not confirm:
        return ("REFUSED (needs confirm=true): this command can delete data or change the device "
                "state. Ask the user first, then call again with confirm=true.")
    return _clip(_sh(command, timeout=timeout_s), 30000)


@mcp.tool()
def phone_info() -> dict:
    """Device model, Android/OS version, security patch, battery, storage, RAM and uptime."""
    p = lambda k: _sh(f"getprop {k}").strip()
    batt = dict(re.findall(r"^\s+(level|status|health|temperature|plugged): (\S+)", _sh("dumpsys battery"), re.M))
    if "temperature" in batt:
        batt["temperature_c"] = int(batt.pop("temperature")) / 10
    mem = dict(re.findall(r"^(MemTotal|MemAvailable):\s+(\d+)", _sh("cat /proc/meminfo"), re.M))
    df = _sh("df -h /data").strip().splitlines()[-1].split()
    return {
        "model": p("ro.product.model"), "device": p("ro.product.device"),
        "android": p("ro.build.version.release"), "security_patch": p("ro.build.version.security_patch"),
        "build": p("ro.build.version.incremental"), "serial": _serial(),
        "battery": batt,
        "ram_gb": {k: round(int(v) / 1048576, 2) for k, v in mem.items()},
        "storage": {"size": df[1], "used": df[2], "free": df[3], "use%": df[4]} if len(df) >= 5 else df,
        "uptime": _sh("uptime").strip(),
    }


@mcp.tool()
def phone_logcat(lines: int = 200, filter: str = "", since_seconds: int = 0) -> str:
    """Recent logcat lines. `filter` is a case-insensitive regex applied on the PC side.
    since_seconds>0 limits to that many recent seconds."""
    cmd = f"logcat -d -t {int(lines) if not since_seconds else 100000}"
    out = _sh(cmd, timeout=90)
    if since_seconds:
        cutoff = time.time() - since_seconds
        year = datetime.now().year
        keep = []
        for l in out.splitlines():
            m = re.match(r"(\d\d-\d\d \d\d:\d\d:\d\d)", l)
            if m and datetime.strptime(f"{year}-{m.group(1)}", "%Y-%m-%d %H:%M:%S").timestamp() >= cutoff:
                keep.append(l)
        out = "\n".join(keep[-lines:])
    if filter:
        rx = re.compile(filter, re.I)
        out = "\n".join(l for l in out.splitlines() if rx.search(l))
    return _clip(out, 30000)


@mcp.tool()
def phone_settings(namespace: str, key: str = "", value: str | None = None) -> str:
    """Read or write Android settings. namespace = system | secure | global.
    No key = list all in the namespace. value given = write it (secure writes are refused; use phone_shell with confirm)."""
    if namespace not in ("system", "secure", "global"):
        return "namespace must be system, secure or global"
    if not key:
        return _clip(_sh(f"settings list {namespace}"), 30000)
    if value is None:
        return _sh(f"settings get {namespace} {_q(key)}").strip()
    if namespace == "secure":
        return "REFUSED: secure settings writes go through phone_shell with confirm=true after asking the user."
    _sh(f"settings put {namespace} {_q(key)} {_q(value)}")
    return f"{namespace}/{key} = " + _sh(f"settings get {namespace} {_q(key)}").strip()


# ================================================================= tools: notifications


@mcp.tool()
def phone_notifications(package: str = "", include_system: bool = False) -> list[dict]:
    """Active notifications with full (unredacted) title and text, newest first. Read-only;
    does not dismiss or open anything. Filter by app package with `package`."""
    out = _sh("dumpsys notification --noredact", timeout=60)
    records = re.split(r"\n\s*NotificationRecord\(", out)[1:]
    res = []
    for rec in records:
        m = re.search(r"pkg=(\S+)", rec)
        pkg = m.group(1) if m else "?"
        if package and pkg != package:
            continue
        if not include_system and pkg in ("android", "com.android.systemui", "com.miui.securitycenter"):
            continue
        get = lambda k: (re.search(rf"{re.escape(k)}=\w+ \((.*?)\)\n", rec, re.S) or [None, None])[1]
        when = re.search(r"\bwhen=(\d+)", rec) or re.search(r"postTime=(\d+)", rec)
        item = {
            "package": pkg,
            "title": get("android.title"),
            "text": get("android.text"),
            "big_text": get("android.bigText"),
            "sub_text": get("android.subText"),
            "conversation": get("android.conversationTitle"),
            "time": _ms(when.group(1)) if when else None,
        }
        if any(item[k] for k in ("title", "text", "big_text")):
            res.append({k: v for k, v in item.items() if v})
    res.sort(key=lambda r: r.get("time") or "", reverse=True)
    return res


# ================================================================= tools: personal data (read-only)


@mcp.tool()
def phone_sms(box: str = "inbox", limit: int = 20, contains: str = "", address: str = "") -> list[dict]:
    """Read SMS messages (read-only). box = inbox | sent | all. Filter by body text or sender address."""
    uri = {"inbox": "content://sms/inbox", "sent": "content://sms/sent", "all": "content://sms"}.get(box)
    if not uri:
        return [{"error": "box must be inbox, sent or all"}]
    where = []
    if contains:
        where.append("body LIKE '%" + contains.replace("'", "''") + "%'")
    if address:
        where.append("address LIKE '%" + address.replace("'", "''") + "%'")
    rows = _content_query(uri, ["_id", "address", "date", "read", "type", "body"],
                          where=" AND ".join(where) or None, sort="date DESC")
    for r in rows:
        r["date"] = _ms(r.get("date"))
    return rows[: max(1, limit)]


@mcp.tool()
def phone_contacts(name_contains: str = "", limit: int = 50) -> list[dict]:
    """Search contacts (name + phone numbers). Read-only."""
    where = ("display_name LIKE '%" + name_contains.replace("'", "''") + "%'") if name_contains else None
    rows = _content_query("content://com.android.contacts/data/phones",
                          ["display_name", "data1", "data2"], where=where, sort="display_name ASC")
    out: dict[str, dict] = {}
    for r in rows:
        c = out.setdefault(r.get("display_name") or "?", {"name": r.get("display_name"), "numbers": []})
        if r.get("data1") and r["data1"] not in c["numbers"]:
            c["numbers"].append(r["data1"])
    return list(out.values())[: max(1, limit)]


@mcp.tool()
def phone_call_log(limit: int = 30, number_contains: str = "") -> list[dict]:
    """Recent calls (read-only). type: 1=incoming 2=outgoing 3=missed 5=rejected 6=blocked. duration in seconds."""
    where = ("number LIKE '%" + number_contains.replace("'", "''") + "%'") if number_contains else None
    rows = _content_query("content://call_log/calls", ["number", "name", "date", "duration", "type"],
                          where=where, sort="date DESC")
    kinds = {"1": "incoming", "2": "outgoing", "3": "missed", "4": "voicemail", "5": "rejected", "6": "blocked"}
    for r in rows:
        r["date"] = _ms(r.get("date"))
        r["type"] = kinds.get(r.get("type"), r.get("type"))
    return rows[: max(1, limit)]


# ================================================================= tools: files & media


@mcp.tool()
def phone_files(path: str = "/sdcard") -> str:
    """List a directory on the phone's shared storage (or any path the shell user may read)."""
    return _clip(_sh(f"ls -la {_q(path)}"), 30000)


@mcp.tool()
def phone_recent_media(kind: str = "images", limit: int = 20) -> list[dict]:
    """Newest photos/videos/audio/downloads from the MediaStore. kind = images | video | audio | downloads."""
    uri = {"images": "content://media/external/images/media", "video": "content://media/external/video/media",
           "audio": "content://media/external/audio/media", "downloads": "content://media/external/downloads"}.get(kind)
    if not uri:
        return [{"error": "kind must be images, video, audio or downloads"}]
    rows = _content_query(uri, ["_display_name", "_data", "_size", "date_added"], sort="date_added DESC")
    for r in rows:
        try:
            r["date_added"] = datetime.fromtimestamp(int(r["date_added"])).isoformat(timespec="seconds")
        except (KeyError, TypeError, ValueError):
            pass
    return rows[: max(1, limit)]


@mcp.tool()
def phone_pull(remote_path: str, local_path: str) -> str:
    """Copy a file or folder from the phone to this PC."""
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    return _transfer("pull", remote_path, local_path)


@mcp.tool()
def phone_push(local_path: str, remote_path: str) -> str:
    """Copy a file or folder from this PC to the phone (e.g. /sdcard/Download/)."""
    return _transfer("push", local_path, remote_path)


def _transfer(verb: str, src: str, dst: str) -> str:
    # adb prints the "N file(s) pushed/pulled" summary on stderr on Windows; return both streams.
    r = subprocess.run([_adb_path(), "-s", _serial(), verb, src, dst], capture_output=True,
                       timeout=600, stdin=subprocess.DEVNULL)
    msg = (r.stdout + r.stderr).decode("utf-8", "replace").replace("\r\n", "\n").strip()
    lines = [l for l in msg.splitlines() if "%]" not in l]  # drop progress lines
    if r.returncode != 0:
        raise RuntimeError("\n".join(lines) or f"adb {verb} failed")
    return "\n".join(lines) or f"{verb} ok"


@mcp.tool()
def phone_screen_record(seconds: int = 10, local_path: str = "") -> str:
    """Record the screen for up to 180 s and save the MP4 on this PC. Returns the local path."""
    seconds = max(1, min(int(seconds), 180))
    remote = f"/sdcard/cellphone_rec_{int(time.time())}.mp4"
    _sh(f"screenrecord --time-limit {seconds} {remote}", timeout=seconds + 30)
    local = local_path or str(Path.home() / "Videos" / Path(remote).name)
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    _adb("pull", remote, local, timeout=300)
    _sh(f"rm -f {remote}")
    return local


# ================================================================= tools: apps


@mcp.tool()
def phone_apps(third_party_only: bool = True, contains: str = "") -> list[str]:
    """Installed app package names."""
    out = _sh("pm list packages" + (" -3" if third_party_only else ""))
    pk = sorted(l.removeprefix("package:").strip() for l in out.splitlines() if l.startswith("package:"))
    return [p for p in pk if contains.lower() in p.lower()]


@mcp.tool()
def phone_app_info(package: str) -> dict:
    """Version, install dates, granted/denied runtime permissions and notification appop for one app."""
    d = _sh(f"dumpsys package {_q(package)}", timeout=60)
    grab = lambda k: (re.search(rf"{k}=(\S+)", d) or [None, None])[1]
    perms = re.findall(r"^\s+(android\.permission\.[A-Z_]+): granted=(true|false)", d, re.M)
    return {
        "package": package, "version": grab("versionName"), "first_install": grab("firstInstallTime"),
        "last_update": grab("lastUpdateTime"),
        "granted": sorted({p for p, g in perms if g == "true"}),
        "denied": sorted({p for p, g in perms if g == "false"}),
        "post_notification_appop": _sh(f"cmd appops get {_q(package)} POST_NOTIFICATION").strip(),
    }


@mcp.tool()
def phone_app_permission(package: str, permission: str, grant: bool) -> str:
    """Grant or revoke a runtime permission (e.g. android.permission.POST_NOTIFICATIONS)."""
    out = _sh(f"pm {'grant' if grant else 'revoke'} {_q(package)} {_q(permission)}").strip()
    now = permission in phone_app_info(package)["granted"]
    return (out + "\n" if out else "") + f"{permission} is now {'GRANTED' if now else 'NOT granted'}"


@mcp.tool()
def phone_appop(package: str, op: str, mode: str) -> str:
    """Set an app-op, e.g. op=POST_NOTIFICATION mode=allow to re-enable an app's notifications.
    mode = allow | ignore | deny | default."""
    if mode not in ("allow", "ignore", "deny", "default"):
        return "mode must be allow, ignore, deny or default"
    _sh(f"cmd appops set {_q(package)} {_q(op)} {mode}")
    return _sh(f"cmd appops get {_q(package)} {_q(op)}").strip()


@mcp.tool()
def phone_open(url: str = "", package: str = "", activity: str = "") -> str:
    """Open a URL (optionally in a specific app, e.g. package=com.android.chrome) or launch an app,
    using an Android intent. No screen taps involved."""
    if url and package == "com.android.chrome":
        cmd = f"am start -n com.android.chrome/com.google.android.apps.chrome.Main -a android.intent.action.VIEW -d {_q(url)}"
    elif url:
        cmd = f"am start -a android.intent.action.VIEW -d {_q(url)}" + (f" -p {_q(package)}" if package else "")
    elif package and activity:
        cmd = f"am start -n {_q(package + '/' + activity)}"
    elif package:
        cmd = f"monkey -p {_q(package)} -c android.intent.category.LAUNCHER 1"
    else:
        return "give url and/or package"
    out = _sh(cmd)
    return "\n".join(l for l in out.splitlines() if l.strip() and not l.lstrip().startswith("bash arg:")).strip()


# ================================================================= tools: Chrome DevTools (page DOM)

_CDP_PORT = int(os.environ.get("CELLPHONE_CDP_PORT", "9333"))


def _cdp_forward() -> None:
    _adb("forward", f"tcp:{_CDP_PORT}", "localabstract:chrome_devtools_remote")


def _cdp_json(path: str, method: str = "GET"):
    _cdp_forward()
    req = urllib.request.Request(f"http://127.0.0.1:{_CDP_PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _tab(tab: str) -> dict:
    tabs = [t for t in _cdp_json("/json/list") if t.get("type") == "page"]
    if not tabs:
        raise RuntimeError("No Chrome tabs. Open Chrome on the phone first.")
    if not tab:
        return tabs[0]
    for t in tabs:
        if t["id"] == tab or tab.lower() in t.get("url", "").lower() or tab.lower() in t.get("title", "").lower():
            return t
    raise RuntimeError(f"No tab matching {tab!r}")


def _cdp_eval(tab: str, expression: str, attempts: int = 4, timeout: int = 12):
    import websocket  # websocket-client

    t = _tab(tab)
    first = [x for x in _cdp_json("/json/list") if x.get("type") == "page"][0]
    if t["id"] != first["id"]:
        # Chrome freezes (or discards) background tabs; bring this one to the front so it can run JS.
        _cdp_json(f"/json/activate/{t['id']}")
        time.sleep(3)
    msg = None
    for attempt in range(attempts):
        try:
            ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=timeout, suppress_origin=True)
            try:
                ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": expression, "returnByValue": True, "awaitPromise": True}}))
                while True:
                    m = json.loads(ws.recv())
                    if m.get("id") == 1:
                        msg = m
                        break
            finally:
                ws.close()
            break
        except (websocket.WebSocketTimeoutException, TimeoutError, ConnectionError, OSError):
            # A discarded tab reloads after activation; give it more time and retry.
            _cdp_json(f"/json/activate/{t['id']}")
            time.sleep(3 + 2 * attempt)
            t = _tab(t["id"])
    if msg is None:
        raise RuntimeError(f"Tab {t.get('title')!r} did not respond. Make sure the phone screen is on and "
                           "Chrome is in front (phone_open with package=com.android.chrome), then retry.")
    res = msg.get("result", {})
    if "exceptionDetails" in res:
        raise RuntimeError(json.dumps(res["exceptionDetails"])[:1000])
    return res.get("result", {}).get("value")


@mcp.tool()
def chrome_tabs() -> list[dict]:
    """Open Chrome tabs on the phone: id, title, url. The first is the most recently active."""
    return [{"id": t["id"], "title": t.get("title"), "url": t.get("url")}
            for t in _cdp_json("/json/list") if t.get("type") == "page"]


@mcp.tool()
def chrome_open_tab(url: str) -> dict:
    """Open a URL in a new Chrome tab on the phone."""
    import websocket

    before = {t["id"] for t in _cdp_json("/json/list") if t.get("type") == "page"}
    try:  # Chrome on Android rejects /json/new; use the browser-level Target.createTarget instead.
        bws = _cdp_json("/json/version")["webSocketDebuggerUrl"]
        ws = websocket.create_connection(bws, timeout=15, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": url}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == 1:
                    break
        finally:
            ws.close()
        if "error" in m:
            raise RuntimeError(m["error"])
    except Exception:
        # Fallback: an Android intent asking Chrome for a new tab.
        _sh("am start -n com.android.chrome/com.google.android.apps.chrome.Main -a android.intent.action.VIEW "
            f"-d {_q(url)} --ez create_new_tab true --es com.android.browser.application_id com.android.chrome")
    for _ in range(10):
        time.sleep(1)
        new = [t for t in _cdp_json("/json/list") if t.get("type") == "page" and t["id"] not in before]
        if new:
            return {"id": new[0]["id"], "url": new[0].get("url")}
    return {"error": "tab did not appear", "url": url}


@mcp.tool()
def chrome_close_tab(tab: str) -> str:
    """Close a Chrome tab. `tab` = tab id, or text found in its url/title."""
    t = _tab(tab)
    _cdp_json(f"/json/close/{t['id']}")
    return f"closed {t.get('title')} {t.get('url')}"


@mcp.tool()
def chrome_activate_tab(tab: str) -> str:
    """Bring a tab to the front. `tab` = tab id, or text found in its url/title."""
    t = _tab(tab)
    _cdp_json(f"/json/activate/{t['id']}")
    return f"activated {t.get('title')} {t.get('url')}"


@mcp.tool()
def chrome_page_text(tab: str = "", max_chars: int = 20000) -> str:
    """Visible text of a Chrome tab (default = most recent tab). Reads the DOM; no screenshot needed."""
    return _clip(str(_cdp_eval(tab, "document.title + '\\n' + location.href + '\\n\\n' + document.body.innerText")), max_chars)


@mcp.tool()
def chrome_links(tab: str = "", contains: str = "", limit: int = 100) -> list[dict]:
    """Links and buttons in a tab with their text, so you can pick one to click by text."""
    js = """[...document.querySelectorAll('a,button,[role=button],[role=link]')].map(e=>({
      text:(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,120), href:e.href||null}))
      .filter(x=>x.text)"""
    items = _cdp_eval(tab, js) or []
    if contains:
        items = [i for i in items if contains.lower() in i["text"].lower()]
    return items[: max(1, limit)]


@mcp.tool()
def chrome_click(text: str = "", selector: str = "", tab: str = "", exact: bool = False) -> str:
    """Click an element in a Chrome tab by its visible text (or aria-label) or by CSS selector.
    Clicking Post/Send/Comment/Like publishes things: only do that when the user asked for it."""
    js = f"""(()=>{{
      const sel={json.dumps(selector)}, txt={json.dumps(text.strip().lower())}, exact={json.dumps(exact)};
      let el=null;
      if(sel) el=document.querySelector(sel);
      else {{
        const c=[...document.querySelectorAll('a,button,[role=button],[role=link],[role=tab],[role=menuitem],input[type=submit],span,div')];
        const t=e=>(e.innerText||e.getAttribute('aria-label')||'').trim().toLowerCase();
        el=c.find(e=>t(e)===txt) || (!exact && c.find(e=>t(e).includes(txt) && e.children.length<4));
      }}
      if(!el) return 'NOT FOUND';
      el.scrollIntoView({{block:'center'}}); el.click();
      return 'clicked: '+(el.innerText||el.getAttribute('aria-label')||el.tagName).trim().slice(0,100);
    }})()"""
    return str(_cdp_eval(tab, js))


@mcp.tool()
def chrome_fill(selector: str, value: str, tab: str = "") -> str:
    """Put text into an input/textarea/contenteditable found by CSS selector. Does NOT submit."""
    js = f"""(()=>{{
      const el=document.querySelector({json.dumps(selector)}); if(!el) return 'NOT FOUND';
      el.focus();
      if(el.isContentEditable) document.execCommand('insertText', false, {json.dumps(value)});
      else {{ const p=Object.getPrototypeOf(el); const d=Object.getOwnPropertyDescriptor(p,'value');
             d&&d.set?d.set.call(el,{json.dumps(value)}):(el.value={json.dumps(value)});
             el.dispatchEvent(new Event('input',{{bubbles:true}})); el.dispatchEvent(new Event('change',{{bubbles:true}})); }}
      return 'filled';
    }})()"""
    return str(_cdp_eval(tab, js))


@mcp.tool()
def chrome_scroll(tab: str = "", pixels: int = 1500) -> str:
    """Scroll a Chrome tab by N pixels (negative = up) and return the new scroll position."""
    return str(_cdp_eval(tab, f"window.scrollBy(0,{int(pixels)}); Math.round(window.scrollY)+' / '+document.body.scrollHeight"))


@mcp.tool()
def chrome_eval(javascript: str, tab: str = "") -> str:
    """Run JavaScript in a Chrome tab and return the JSON result (power tool)."""
    return _clip(json.dumps(_cdp_eval(tab, javascript), ensure_ascii=False, default=str), 30000)


if __name__ == "__main__":
    mcp.run()
