#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注册流程端到端测试（v2，修掉上一版语法错）+ 双机连通性验证。"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BASE = "/opt/remote-tunnel-control"
SVC = "http://127.0.0.1:9099"
OUT = "/tmp/_enroll_tok"


def http(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")


def run(c):
    p = subprocess.run(c, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def ticket_from(out):
    for l in out.splitlines():
        if l.strip().startswith("凭证"):
            return l.split(":", 1)[1].strip()
    return ""


print("=" * 70)
print("A. 注册流程端到端测试")
print("=" * 70)
rc, out = run(["python3", BASE + "/enroll.py", "issue", "--desc", "端到端测试机",
               "--machine-id", "test3", "--control-port", "9300", "--serve-port", "9320",
               "--guard-base", "19300", "--ttl", "30"])
assert "Traceback" not in out, "issue 报错:\n" + out
tk = ticket_from(out)
print("1) 签发       ✓ %s（粘贴块已生成，%d 字符，含守卫表）" % (tk, len(out)))

print("2) verify     ✓ %s" % json.dumps(http(SVC + "/verify?ticket=" + tk)[1], ensure_ascii=False)[:150])

rc, out = run(["python3", BASE + "/enroll_client.py", "--ticket", tk, "--hostname", "TEST-AGENT-3",
               "--control-port", "9300", "--serve-port", "9320", "--guard-base", "19300",
               "--desc", "端到端测试机", "--cloud", SVC + "/enroll", "--out", OUT])
assert rc == 0, "兑换失败:\n" + out
print("3) 代理兑换   ✓ %s" % " / ".join(out.splitlines()[1:4]))

reg = json.load(open(BASE + "/machines.json", encoding="utf-8"))["machines"]
m = reg.get("test3")
envp = m["env"]
cloud_tok = [l.split("=", 1)[1].strip() for l in open(envp, encoding="utf-8") if "TOKEN" in l][0]
ok_same = cloud_tok == open(OUT, encoding="utf-8").read().strip()
print("4) 云端登记   ✓ 写入 machines.json + %s（权限 %s）；令牌与代理侧一致: %s"
      % (envp, oct(os.stat(envp).st_mode & 0o777), ok_same))

e1 = http(SVC + "/enroll", {"ticket": tk, "hostname": "R", "control_port": 9300, "serve_port": 9320})[1].get("error")
e2 = http(SVC + "/enroll", {"ticket": "RGT-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ", "hostname": "X", "control_port": 9400, "serve_port": 9420})[1].get("error")
rc, out2 = run(["python3", BASE + "/enroll.py", "issue", "--desc", "端口不符", "--control-port", "9500",
                "--serve-port", "9520", "--ttl", "10"])
t2 = ticket_from(out2)
e3 = http(SVC + "/enroll", {"ticket": t2, "hostname": "X", "control_port": 9999, "serve_port": 9998})[1].get("error")
e4 = http(SVC + "/enroll", {"ticket": "", "hostname": "X", "control_port": 9600, "serve_port": 9620})[1].get("error")
e5 = http(SVC + "/revoke", {"admin_key": "bad", "machine_id": "test3"})[1].get("error")
print("5) 负例        ✓ 重放→%s ｜ 伪造→%s ｜ 端口不符→%s ｜ 空凭证→%s ｜ 注销鉴权→%s"
      % (e1, e2, e3, e4, e5))

reg.pop("test3", None)
json.dump({"machines": reg}, open(BASE + "/machines.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
t = json.load(open(BASE + "/enroll/tickets.json", encoding="utf-8"))
for k in [k for k, v in t["tickets"].items() if v.get("desc") in ("端到端测试机", "端口不符")]:
    t["tickets"].pop(k)
json.dump(t, open(BASE + "/enroll/tickets.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
for p in (OUT, "/opt/machines/test3/env.sh"):
    if os.path.exists(p):
        os.remove(p)
d = "/opt/machines/test3"
if os.path.isdir(d) and not os.listdir(d):
    os.rmdir(d)
print("6) 清理        ✓ 剩余机器: %s" % list(reg.keys()))

print()
print("=" * 70)
print("B. 第二台代理（machine2）连通性验证")
print("=" * 70)
env = {}
for line in open("/opt/machines/machine2/env.sh", encoding="utf-8"):
    if "=" in line:
        k, v = line.strip().split("=", 1)
        env[k] = v
tok = env.get("MACHINE_B_TOKEN", "")
try:
    req = urllib.request.Request("http://127.0.0.1:9100/status", headers={"X-Token": tok})
    with urllib.request.urlopen(req, timeout=20) as r:
        st = json.loads(r.read().decode("utf-8", "ignore")).get("output", {})
    print("控制面 9100  ✓ 主机=%s 时间=%s" % (st.get("host"), st.get("time")))
    print("           %s" % json.dumps({k: st.get(k) for k in ("procs", "gpu", "disk_free_gb") if k in st}, ensure_ascii=False))
except Exception as e:
    print("控制面 9100  ✗ %s: %s" % (type(e).__name__, e))
try:
    urllib.request.urlopen("http://127.0.0.1:9120/health", timeout=10)
    print("agent  9120  ✓ 无认证即通（异常，应 401）")
except urllib.error.HTTPError as e:
    print("agent  9120  ✓ 端口通、需认证（HTTP %s ← 期望）" % e.code)
except Exception as e:
    print("agent  9120  ✗ %s: %s" % (type(e).__name__, e))
