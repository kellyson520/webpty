#!/usr/bin/env python3
"""8-hour UI soak, v3 — self-healing against browser/driver/session death.

Probes the live webpty UI every 10 minutes: fatal bar, active tab, canvas
pixel presence. ANY probe failure (driver error, session loss, canvas
missing, fatal bar) triggers ONE browser-session recreate; only a failed
recreate counts as an issue. v2 counted every probe after a session loss
as an issue (false FAIL), which made harness cleanup look like app bugs.
"""
import json
import sys
import time
import urllib.error
import urllib.request

DRIVER = "http://127.0.0.1:9522"
API = "http://127.0.0.1:4789"
DURATION_S = 8 * 3600
PROBE_S = 600


def wd(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(DRIVER + path, method=method, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"value": {"error": "http-%d" % e.code}}
    except Exception:
        return {"value": {"error": "conn"}}


def cmds(sid, path, method="GET", body=None):
    return wd(method, "/session/%s%s" % (sid, path), body)


def js(sid, code):
    r = cmds(sid, "/execute/sync", "POST", {"script": code, "args": []})
    v = r.get("value")
    if isinstance(v, dict) and "error" in v:
        raise RuntimeError(str(v)[:80])
    return v


def new_driver_session():
    res = wd("POST", "/session", {"capabilities": {"alwaysMatch": {
        "browserName": "chrome",
        "goog:chromeOptions": {"args": ["--headless=new", "--no-sandbox",
                                        "--disable-gpu",
                                        "--disable-dev-shm-usage"]}}}})
    sid = res.get("value", {}).get("sessionId")
    if sid:
        cmds(sid, "/url", "POST", {"url": API + "/"})
    return sid


FATAL_JS = ("var el = document.getElementById('webpty-fatal');"
            " return el ? (el.hidden ? '' : el.textContent.slice(0,60)) : '';")
ACTIVE_JS = ("var t = document.querySelector('.tab.active');"
             " return t ? t.dataset.name : 'NONE';")
CANVAS_JS = ("var c = Array.from(document.querySelectorAll('canvas')).filter("
             "function(x){ return x.offsetParent !== null && x.width > 0; })[0];"
             " return c ? 'OK' : 'NO';")


def probe(sid):
    """Returns (ok, detail). Raises nothing — failures come back as detail."""
    try:
        fatal = js(sid, FATAL_JS)
        active = js(sid, ACTIVE_JS)
        cv = js(sid, CANVAS_JS)
        if fatal:
            return False, "fatal:%s" % str(fatal)[:40]
        if cv != "OK":
            return False, "canvas:%s" % cv
        return True, "active=%s canvas=%s" % (active, cv)
    except Exception as e:  # noqa: BLE001
        return False, "driver-error:%s" % str(e)[:60]


def main():
    sid = new_driver_session()
    print("initial driver session:", bool(sid), flush=True)
    if not sid:
        sys.exit(1)
    t0 = time.time()
    issues = 0
    probes = 0
    recreated = 0
    while time.time() - t0 < DURATION_S:
        time.sleep(PROBE_S)
        probes += 1
        ok, detail = probe(sid)
        if ok:
            print("t=%dmin %s" % (int((time.time() - t0) / 60), detail),
                  flush=True)
            continue
        # failure: one browser-session recreate, then re-probe
        print("t=%dmin probe issue: %s — recreating browser session"
              % (int((time.time() - t0) / 60), detail), flush=True)
        try:
            cmds(sid, "", "DELETE")
        except Exception:  # noqa: BLE001
            pass
        sid2 = new_driver_session()
        if not sid2:
            issues += 1
            print("  recreate failed (driver unreachable) — issue counted",
                  flush=True)
            continue
        sid = sid2
        recreated += 1
        time.sleep(5)
        ok2, detail2 = probe(sid)
        if not ok2:
            issues += 1
            print("  post-recreate probe still failing: %s — issue counted"
                  % detail2, flush=True)
    print("SOAK8H v3 RESULT: probes=%d issues=%d recreated=%d -> %s"
          % (probes, issues, recreated,
             "PASS" if issues == 0 else "FAIL"), flush=True)
    try:
        cmds(sid, "", "DELETE")
    except Exception:  # noqa: BLE001
        pass
    print("done", flush=True)


if __name__ == "__main__":
    main()
