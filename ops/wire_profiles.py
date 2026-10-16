#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「运行时画像」接进 agentsdb / rmt.py / machines.json，然后真机探测两台机器。"""
import json
import os
import subprocess
import sys

BASE = "/opt/remote-tunnel-control"


def patch(path, pairs, tag):
    p = os.path.join(BASE, path)
    src = open(p, encoding="utf-8").read()
    for old, new in pairs:
        if old not in src and new.strip().split("\n")[0] in src:
            continue
        if src.count(old) != 1:
            print("  [SKIP/ABORT] %s：锚点出现 %d 次（%s）" % (path, src.count(old), tag))
            return False
        src = src.replace(old, new, 1)
    open(p + ".bak-profile", "w", encoding="utf-8").write(open(p, encoding="utf-8").read())
    open(p, "w", encoding="utf-8", newline="").write(src)
    print("  [OK] %s ← %s" % (path, tag))
    return True


print("=== 1) agentsdb：画像表 + agents.runtime 列 + 迁移 ===")
p = os.path.join(BASE, "agentsdb.py")
s = open(p, encoding="utf-8").read()
if "runtime_profiles" not in s:
    s = s.replace('''CREATE INDEX IF NOT EXISTS idx_sessions_machine''',
                  '''CREATE TABLE IF NOT EXISTS runtime_profiles (
  machine_id TEXT PRIMARY KEY, runtime TEXT, action TEXT, action_tried TEXT,
  features TEXT, timings TEXT, samples TEXT, source TEXT, probed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_sessions_machine''', 1)
    s = s.replace('''def init(db=DB):
    con = connect(db)
    con.executescript(SCHEMA)
    con.commit()
    return con''',
                  '''def _migrate(con):
    """轻量迁移：给老库补列（幂等）。"""
    for tbl, col in (("agents", "runtime"), ("agents", "agent_action"),
                     ("sessions", "runtime"), ("sessions", "action")):
        cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % tbl)]
        if col not in cols:
            con.execute("ALTER TABLE %s ADD COLUMN %s TEXT" % (tbl, col))
            print("[agentsdb] 迁移: %s.%s 已添加" % (tbl, col))


def init(db=DB):
    con = connect(db)
    con.executescript(SCHEMA)
    _migrate(con)
    con.commit()
    return con


def save_profile(profile):
    """写入/更新运行时画像（探测或自述的结果）。"""
    con = init()
    con.execute("""INSERT OR REPLACE INTO runtime_profiles
        (machine_id,runtime,action,action_tried,features,timings,samples,source,probed_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (profile.get("machine_id"), profile.get("runtime"), profile.get("action"),
         json.dumps(profile.get("action_tried"), ensure_ascii=False),
         json.dumps(profile.get("features"), ensure_ascii=False),
         json.dumps(profile.get("timings"), ensure_ascii=False),
         json.dumps(profile.get("samples"), ensure_ascii=False),
         profile.get("source"), profile.get("probed_at")))
    con.commit()
    con.close()


def get_profile(machine_id):
    """读画像 → dict（含 action / features / timings）；没有则 {}"""
    con = init()
    r = con.execute("SELECT * FROM runtime_profiles WHERE machine_id=?", (machine_id,)).fetchone()
    con.close()
    if not r:
        return {}
    d = dict(r)
    for k in ("action_tried", "features", "timings", "samples"):
        try:
            d[k] = json.loads(d[k] or "null") or {}
        except Exception:
            d[k] = {}
    return d''', 1)
    s = s.replace('''    p = sub.add_parser("stats")''',
                  '''    sub.add_parser("profiles").set_defaults(
        fn=lambda a: [(print("-" * 60), print("机器 %s 运行时 %s 动作 /%s 通道=%s 工具=%s 建议超时=%s"
                                              % (r["machine_id"], r["runtime"], r["action"],
                                                 (r["features"] or {}).get("task_channel"),
                                                 (r["features"] or {}).get("tools"),
                                                 (r["features"] or {}).get("suggested_timeout"))))
                      for r in [dict(x, features=json.loads(x["features"] or "{}"))
                                for x in init().execute("SELECT * FROM runtime_profiles").fetchall()]]
        and 0)
    p = sub.add_parser("stats")''', 1)
    open(p, "w", encoding="utf-8", newline="").write(s)
    print("  [OK] agentsdb.py ← 画像表 + 迁移 + save/get_profile")
else:
    print("  [SKIP] 已接入")

print("\n=== 2) agentsdb：同步注册表时带上 runtime ===")
patch("agentsdb.py", [(
    '''        upsert_agent(mid, hostname=m.get("hostname"), desc=m.get("desc"),''',
    '''        upsert_agent(mid, hostname=m.get("hostname"), desc=m.get("desc"),''')], "（占位，不改）")

print("\n=== 3) rmt.py：按画像派活 + probe/profile 子命令 + list 显示运行时 ===")
rt = os.path.join(BASE, "rmt.py")
s = open(rt, encoding="utf-8").read()
if "get_profile" not in s:
    s = s.replace('''def cmd_run(name, m, task, tmo):
    body = json.dumps({"task": task, "mode": "print", "timeout": tmo}).encode("utf-8")
    t0 = time.time()
    try:
        d = call(m, "reasonix", data=body, tmo=tmo + 120)''',
                  '''def cmd_run(name, m, task, tmo):
    # ① 有画像就按画像派活（动作名 + 建议超时），没有则回退到候选动作
    prof, action = {}, None
    try:
        import agentsdb as adb
        prof = adb.get_profile(name) or {}
        action = prof.get("action") or None
        if prof.get("features", {}).get("suggested_timeout") and tmo <= 180:
            tmo = int(prof["features"]["suggested_timeout"])
        if prof and not prof.get("features", {}).get("task_channel", True):
            print("✗ 该机画像显示未启用任务通道（%s）" % prof.get("features", {}).get("reason", "n/a"))
    except Exception:                                     # noqa: BLE001
        pass
    candidates = [action] if action else ["agent", "task", "run", "reasonix"]
    body = json.dumps({"task": task, "mode": "print", "timeout": tmo}).encode("utf-8")
    t0 = time.time()
    last_err = None
    d = None
    for act in candidates:                                # ② 动作名逐个试（兼容不同运行时）
        try:
            d = call(m, act, data=body, tmo=tmo + 120)
            action = act
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (404, 405, 501):                 # 该动作不存在 → 试下一个
                continue
            break
    if d is None and last_err is None:
        raise RuntimeError("无可用的任务动作")
    if d is None:
        e = last_err
        detail = e.read().decode("utf-8", "ignore")[:300]''', 1)
    s = s.replace('''    o = d.get("output", {})
    print("rc=%s  用时 %ss" % (o.get("rc"), o.get("secs")))''',
                  '''    o = d.get("output", {})
    if action:
        print("(动作 /%s%s)" % (action, "，按画像" if prof else ""))
    print("rc=%s  用时 %ss" % (o.get("rc"), o.get("secs")))''', 1)
    s = s.replace('''        sid = adb.log_session(name, "reasonix_task", title=task[:80],
                              status="ok" if o.get("rc") == 0 else "failed",
                              duration_s=o.get("secs") or round(time.time() - t0, 1))
        adb.log_message(sid, "user", task)''',
                  '''        sid = adb.log_session(name, "agent_task", title=task[:80],
                              status="ok" if o.get("rc") == 0 else "failed",
                              duration_s=o.get("secs") or round(time.time() - t0, 1))
        adb.log_message(sid, "user", task)''', 1)
    s = s.replace('''            sid = adb.log_session(name, "reasonix_task", title=task[:80], status="failed",''',
                  '''            sid = adb.log_session(name, "agent_task", title=task[:80], status="failed",''', 1)
    # 子命令
    s = s.replace('''    print("未知操作 %r（见 --help 的用法）" % op)''',
                  '''    if op == "probe":
        import agent_probe
        agent_probe.probe(name)
        return 0
    if op == "profile":
        import agent_probe
        agent_probe.show(name)
        return 0
    print("未知操作 %r（见 --help 的用法）" % op)''', 1)
    # list 显示运行时
    s = s.replace('''    print("%-12s %-7s %-6s %s" % ("机器", "状态", "端口", "说明"))
    for name, m in sorted(mm.items()):
        ok = online(m)
        print("%-12s %-7s %-6s %s" % (name, "在线" if ok else "离线",
                                      m.get("control_port"), m.get("desc", "")))''',
                  '''    print("%-12s %-7s %-6s %-16s %s" % ("机器", "状态", "端口", "运行时", "说明"))
    for name, m in sorted(mm.items()):
        ok = online(m)
        print("%-12s %-7s %-6s %-16s %s" % (name, "在线" if ok else "离线",
              m.get("control_port"), m.get("runtime", "-"), m.get("desc", "")))''', 1)
    open(rt, "w", encoding="utf-8", newline="").write(s)
    print("  [OK] rmt.py ← 画像派活 + probe/profile + list 显示运行时")
else:
    print("  [SKIP] 已接入")

print("\n=== 4) machines.json：登记各机运行时 ===")
regp = os.path.join(BASE, "machines.json")
reg = json.load(open(regp, encoding="utf-8"))
reg["machines"]["machine-a"].update({"runtime": "reasonix", "agent_action": "reasonix",
                                 "action_candidates": ["agent", "task", "run", "reasonix"]})
reg["machines"]["machine2"].update({"runtime": "kimi-sandbox",
                                    "agent_action": None,
                                    "action_candidates": ["agent", "task", "run", "reasonix"],
                                    "notes": "Kimi 沙箱；当前为最小控制面，未启用任意任务通道"})
json.dump(reg, open(regp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("  " + json.dumps({k: v.get("runtime") for k, v in reg["machines"].items()}, ensure_ascii=False))

print("\n=== 5) 语法检查 ===")
for f in ("agentsdb.py", "rmt.py", "agent_probe.py", "agent_adapters.py", "agent_bridge.py"):
    r = subprocess.run([sys.executable, "-m", "py_compile", os.path.join(BASE, f)],
                       capture_output=True, text=True)
    print("  %-20s %s" % (f, "OK" if r.returncode == 0 else "FAIL " + r.stderr[:160]))
