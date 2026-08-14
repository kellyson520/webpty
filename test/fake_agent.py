#!/usr/bin/env python3
"""Fake stream-json agent CLI for integration tests.

Speaks the claude stream-json protocol subset the agent engine consumes:
system/init, assistant (text blocks), result. Echoes user messages.
Ignores all argv (the engine passes claude flags).
"""
import json
import sys
import threading
import time

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

emit({"type": "system", "subtype": "init",
      "session_id": "fake-sess-1", "model": "fake-model",
      "cwd": ".", "permissionMode": "bypassPermissions"})

def reader():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
            text = (m.get("message") or {}).get("content", "")
            emit({"type": "assistant", "message": {
                "id": "fake-m",
                "content": [{"type": "text", "text": "GOT: " + str(text) + "\n"}]}})
            emit({"type": "result", "session_id": "fake-sess-1",
                  "model": "fake-model"})
        except Exception:
            pass

threading.Thread(target=reader, daemon=True).start()

n = 0
while True:
    time.sleep(1)
    n += 1
    emit({"type": "assistant", "message": {
        "id": "fake-tick",
        "content": [{"type": "text", "text": "TICK%d\n" % n}]}})
    emit({"type": "result", "session_id": "fake-sess-1", "model": "fake-model"})
