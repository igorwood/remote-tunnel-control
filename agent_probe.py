#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_probe.py —— 【云端】探测并"学会"如何操作每台代理上的 agent

不假设任何具体运行时（Kimi / GLM / Claude / GPT / Gemini / 自研…）。做法：
  1) **动作发现**：依次尝试候选动作名（agent → task → run → reasonix），找出该机控制面真正实现的那个
  2) **能力探测**（只用安全、只读的任务）：
       P1 基本调用    : "只回复 OK"                      → 能否调用、返回值是否正确
       P2 工具能力    : "报告当前工作目录/机器名/时间"      → 是否具备工具调用（能读环境）
       P3 延迟基准    : 连续 3 次 P1，取中位耗时           → 超时预算与速率
       P4 长任务行为  : 给一个 30s 的思考任务               → 记录是否超时/截断
       P5 身份自述    : "你是什么模型/运行时，版本"         → 供画像记录
  3) **落库画像**：把结论写进 agentsdb.runtime_profiles（来源 probe / self-report）
     后续 rmt.py 派活时直接读画像：用哪个动作、超时给多少、该机器擅长什么

用法：
    python3 agent_probe.py <machine>          # 探测一台并写入画像
    python3 agent_probe.py --all              # 探测全部（跳过多余重复探测）
    python3 agent_probe.py --show [machine]   # 只看已有画像
    python3 agent_probe.py --self-report <machine> <json文件>   # 并入代理自述的能力卡片
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE = "/opt/remote-tunnel-control"
sys.path.insert(0, BASE)
import agentsdb as adb  # noqa: E402

REGISTRY = os.path.join(BASE, "machines.json")
ACTION_CANDIDATES = ["agent", "task", "run", "reasonix"]     # 动作发现顺序

PROBES = [
    ("P1_basic",   "只回复 OK 两个字母，不要做其他任何事",              60),
    ("P2_tools",   "报告你所在机器的主机名、当前工作目录和当前时间",      90),
    ("P4_long",    "请先在心里想清楚再回答：1+1 等于几？只回复数字。",    120),
    ("P5_identity", "用一句话说明你是什么模型或运行时、什么版本。",       90),
]


def load_env(path):
    env = {}
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line[7:] if line.startswith("export ") else line
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def registry():
    return json.load(open(REGISTRY, encoding="utf-8")).get("machines", {})


def call_control(m, path, data=None, tmo=40):
    tok = load_env(m.get("env")).get(m.get("token_key", ""), "")
    url = "http://127.0.0.1:%s/%s" % (m["control_port"], path)
    req = urllib.request.Request(url, data=data,
                                 headers={"X-Token": tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=tmo) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def try_action(m, action, task, timeout):
    """对某动作发一次任务，返回 (ok, result_dict_or_error)"""
    body = json.dumps({"task": task, "mode": "print", "timeout": timeout}).encode("utf-8")
    t0 = time.time()
    try:
        d = call_control(m, action, data=body, tmo=timeout + 60)
        o = d.get("output", {}) if isinstance(d, dict) else {}
        return True, {"rc": o.get("rc"), "output": (o.get("output") or "")[:4000],
                      "secs": o.get("secs") or round(time.time() - t0, 1),
                      "raw_ok": d.get("ok", True)}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        return False, {"http": e.code, "error": detail, "secs": round(time.time() - t0, 1)}
    except Exception as e:                                   # noqa: BLE001
        return False, {"error": "%s: %s" % (type(e).__name__, e),
                       "secs": round(time.time() - t0, 1)}


def probe(machine_id, verbose=True):
    reg = registry()
    if machine_id not in reg:
        print("未知机器: %s" % machine_id)
        return None
    m = reg[machine_id]
    rt = m.get("runtime") or "unknown"
    print("=" * 74)
    print("探测 %s（登记运行时: %s，隧道端口 %s）" % (machine_id, rt, m["control_port"]))
    print("=" * 74)

    profile = {"machine_id": machine_id, "runtime": rt, "action": None, "action_tried": [],
               "features": {}, "timings": {}, "samples": {}, "source": "probe",
               "probed_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # ---- ① 动作发现 ----
    ok_action, first = None, None
    for act in ACTION_CANDIDATES:
        ok, res = try_action(m, act, PROBES[0][1], PROBES[0][2])
        profile["action_tried"].append({"action": act, "ok": ok,
                                        "http": res.get("http"), "err": (res.get("error") or "")[:120]})
        if ok:
            ok_action, first = act, res
            print("  ① 动作发现: ✓ /%s 可用（用时 %ss）" % (act, res.get("secs")))
            for a2 in profile["action_tried"][:-1]:
                print("     · /%s 不可用: HTTP %s %s" % (a2["action"], a2["http"], a2["err"][:60]))
            break
        print("     · /%s 不可用: HTTP %s %s" % (act, res.get("http"), (res.get("error") or "")[:70]))
    if not ok_action:
        profile["features"]["task_channel"] = False
        profile["features"]["reason"] = "所有候选动作都不可用（该机未启用任务通道）"
        print("  ① 动作发现: ✗ 该机未启用任务通道 → 只保留只读控制面")
        adb.save_profile(profile)
        return profile

    profile["action"] = ok_action
    profile["features"]["task_channel"] = True
    profile["samples"]["P1_basic"] = {"rc": first.get("rc"), "secs": first.get("secs"),
                                      "output": (first.get("output") or "")[:200]}

    # ---- ② 能力探测 ----
    for name, task, tmo in PROBES[1:]:
        ok, res = try_action(m, ok_action, task, tmo)
        head = (res.get("output") or res.get("error") or "")[:160].replace("\n", " ")
        print("  · %-11s %s  %ss  %s" % (name, "✓" if ok and res.get("rc") == 0 else "✗",
                                         res.get("secs"), head))
        profile["samples"][name] = {"ok": bool(ok and res.get("rc") == 0),
                                    "secs": res.get("secs"),
                                    "output": (res.get("output") or res.get("error") or "")[:400]}
        if ok and res.get("secs"):
            profile["timings"][name] = res["secs"]
    profile["features"]["tools"] = bool(profile["samples"].get("P2_tools", {}).get("ok"))
    profile["features"]["identity"] = (profile["samples"].get("P5_identity", {}).get("output") or "")[:200]

    # ---- ③ 延迟基准（3 次 P1）----
    lat = []
    for i in range(3):
        ok, res = try_action(m, ok_action, PROBES[0][1], PROBES[0][2])
        if ok and res.get("secs"):
            lat.append(res["secs"])
    if lat:
        med = statistics.median(lat)
        profile["timings"]["P1_median"] = round(med, 2)
        profile["features"]["suggested_timeout"] = int(max(60, med * 20))
        print("  ③ 延迟基准: 中位 %.2fs（样本 %s）→ 建议超时 ≥ %ss"
              % (med, [round(x, 1) for x in lat], profile["features"]["suggested_timeout"]))

    adb.save_profile(profile)
    print("  ④ 画像已写入 agentsdb.runtime_profiles（机器 %s）" % machine_id)
    return profile


def show(machine=None):
    con = adb.init()
    q = "SELECT * FROM runtime_profiles"
    rows = con.execute(q + (" WHERE machine_id=?" if machine else ""),
                       ([machine] if machine else [])).fetchall()
    if not rows:
        print("(暂无画像；先跑 `python3 agent_probe.py <machine>`）")
        return
    for r in rows:
        f = json.loads(r["features"] or "{}")
        print("-" * 74)
        print("机器 %-10s 运行时 %-16s 动作 /%-9s 探测时间 %s" %
              (r["machine_id"], r["runtime"], r["action"], r["probed_at"]))
        print("  任务通道: %-5s 工具能力: %-5s 建议超时: %-5s" %
              (f.get("task_channel"), f.get("tools"), f.get("suggested_timeout")))
        if f.get("identity"):
            print("  自述身份: %s" % f["identity"][:120])
        if f.get("reason"):
            print("  备注: %s" % f["reason"])
        print("  试过的动作: %s" % ", ".join("%s(%s)" % (a["action"], "ok" if a["ok"] else a["http"])
                                              for a in json.loads(r["action_tried"] or "[]")))


def self_report(machine_id, json_file):
    """并入代理自述的能力卡片（agent_bridge.py --self-test 的输出）。"""
    d = json.load(open(json_file, encoding="utf-8"))
    prof = {"machine_id": machine_id, "runtime": d.get("runtime") or "unknown",
            "action": d.get("action") or "agent",
            "action_tried": [], "features": {"self_test": d, "task_channel": True,
                                             "tools": d.get("tools"), "version": d.get("version")},
            "timings": {}, "samples": {}, "source": "self-report",
            "probed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    adb.save_profile(prof)
    print("已并入自述画像: %s（runtime=%s）" % (machine_id, prof["runtime"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("machine", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--self-report", nargs=2, metavar=("MACHINE", "JSON_FILE"))
    a = ap.parse_args()
    if a.show:
        return show(a.machine) or 0
    if a.self_report:
        return self_report(a.self_report[0], a.self_report[1]) or 0
    if a.all:
        for mid in registry():
            probe(mid)
        return 0
    if not a.machine:
        ap.error("需要 machine 或 --all/--show")
    probe(a.machine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
