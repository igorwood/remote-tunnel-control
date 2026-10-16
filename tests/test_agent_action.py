#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自测：环境推断 + 任务桥 + 统一动作契约（/agent /task /ping /status /404 /401）。"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "/opt/remote-tunnel-control"
RT = "/tmp/rt_test.json"
PORT = 9098

with open(RT, "w", encoding="utf-8") as fh:
    json.dump({"default": "echo",
               "echo": {"type": "cli_stdin",
                        "cmd": ["python3", "-c",
                                "import sys;print('BRIDGE-OK <- '+sys.stdin.read().strip())"],
                        "label": "测试用 echo 运行时"}}, fh, ensure_ascii=False)

print("=" * 74)
print("① 环境推断 --detect（机器自报有什么运行时）")
print("=" * 74)
p = subprocess.run([sys.executable, BASE + "/agent_adapters.py", "--detect"],
                   capture_output=True, text=True, timeout=60)
print(p.stdout[:1500] or p.stderr[:600])

print("=" * 74)
print("② 任务桥直跑（指定运行时）")
print("=" * 74)
p = subprocess.run([sys.executable, BASE + "/agent_bridge.py", "--task", "hello from cloud",
                    "--runtime-file", RT], capture_output=True, text=True, timeout=90)
print("  " + (p.stdout or p.stderr).strip()[:300])

print("=" * 74)
print("③ 统一动作参考实现：起服务 + 契约全测")
print("=" * 74)
srv = subprocess.Popen([sys.executable, BASE + "/agent_action.py", "--serve",
                        "--port", str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.5)


def call(path, data=None, headers=None, method=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:                                  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


try:
    st, b = call("/ping")
    print("  · GET  /ping            → HTTP %s  %s" % (st, b[:80]))
    st, b = call("/status")
    print("  · GET  /status          → HTTP %s  %s" % (st, b[:110]))
    st, b = call("/agent", {"task": "契约测试", "runtime_file": RT})
    d = json.loads(b) if st == 200 else {}
    o = d.get("output", {})
    print("  · POST /agent           → HTTP %s  ok=%s rc=%s runtime=%s adapter=%s"
          % (st, d.get("ok"), o.get("rc"), o.get("runtime"), o.get("adapter")))
    print("       output = %r" % (o.get("output") or "")[:80])
    st, b = call("/task", {"task": "别名测试", "runtime_file": RT})
    print("  · POST /task （别名）    → HTTP %s" % st)
    st, b = call("/reasonix", {"task": "旧名兼容", "runtime_file": RT})
    print("  · POST /reasonix（旧名） → HTTP %s  ← 兼容旧机器" % st)
    st, b = call("/bogus", {"task": "x"})
    print("  · POST /bogus（未知）    → HTTP %s  %s  ← 云端据此跳过该动作" % (st, b[:60]))
    st, b = call("/agent", None, method="GET")
    print("  · GET  /agent（方法错）  → HTTP %s" % st)
finally:
    srv.terminate()
    srv.wait(timeout=10)
    print("  （测试服务已停）")

print("=" * 74)
print("④ 语法检查")
print("=" * 74)
for f in ("agent_adapters.py", "agent_bridge.py", "agent_action.py", "agent_probe.py"):
    r = subprocess.run([sys.executable, "-m", "py_compile", BASE + "/" + f],
                       capture_output=True, text=True)
    print("  %-20s %s" % (f, "OK" if r.returncode == 0 else "FAIL " + r.stderr[:150]))
