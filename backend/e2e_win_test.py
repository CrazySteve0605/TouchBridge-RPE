# -*- coding: utf-8 -*-
"""E2E: backend + collector_win --osctest 验证 Windows 采集协议端到端可用。

用法（在仓库根目录）：
  1) 先启动后端：  backend\\venv\\Scripts\\python backend\\app.py
  2) 另开窗口跑：  backend\\venv\\Scripts\\python backend\\e2e_win_test.py
     （需要 requests：backend\\venv\\Scripts\\pip install requests，仅测试用，不在 requirements.txt 里）

预期输出最后两行：
  === note_count after recording: 2 ===
  E2E EXIT: 0
（--osctest 发出的 /tap 与 /up(920ms) 会被还原为 1 个 Tap + 1 个 Hold）
"""
import subprocess
import sys
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import socketio

sio = socketio.Client()

events = {"state": None, "recorded": 0}

@sio.on("state")
def on_state(data):
    events["state"] = data

@sio.on("toast")
def on_toast(data):
    print("toast:", data.get("msg", "")[:80])

@sio.event
def connect():
    print("socketio connected")

# 等后端起来
for i in range(40):
    try:
        urllib.request.urlopen("http://127.0.0.1:8765/", timeout=1)
        break
    except Exception:
        time.sleep(0.5)
else:
    print("backend did not start")
    sys.exit(1)

sio.connect("http://127.0.0.1:8765", transports=["polling"])
time.sleep(0.3)

# 清空 + 开始监听 OSC（等价于网页上点「开始监听 OSC」）+ 开始录制
sio.emit("clear_events")
time.sleep(0.2)
sio.emit("monitor_start")
time.sleep(0.6)
sio.emit("record_start")
time.sleep(0.3)

# 运行 Windows 采集器自检（发真实 OSC 包）
r = subprocess.run([sys.executable, r"TouchBridge\collector_win.py", "--osctest"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=30)
print("collector:", r.stdout.strip().splitlines()[-2:] if r.stdout else r.stderr[-300:])
time.sleep(0.6)

sio.emit("record_stop")
time.sleep(0.5)
sio.emit("get_state")
time.sleep(0.5)

st = events["state"] or {}
n = st.get("note_count", 0)
print(f"\n=== note_count after recording: {n} ===")
sio.disconnect()
sys.exit(0 if n > 0 else 1)
