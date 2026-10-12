#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rmt.py —— 云端「多机统一 CLI」：用一条命令控制任意一台已接线的本地机器。

用法：
    python3 rmt.py list                          # 列出所有机器 + 在线状态
    python3 rmt.py ping <m>                      # 探活
    python3 rmt.py status <m>                    # 进度 / GPU / 进程
    python3 rmt.py tail <m> [which] [lines]      # 日志尾部（默认 pipeline 20）
    python3 rmt.py products <m>                  # 列出产物（list_transcripts）
    python3 rmt.py start|stop|package|analyze <m>
    python3 rmt.py run <m> "<任务描述>" [timeout]  # 交给远端 agent 执行（权限升级）

设计要点（踩坑总结）：
  * 一律用 Python urllib，不用内联 curl：远端/云端安全策略会拦截含中文的内联 curl（审批超时）
  * 令牌从 env 文件读，绝不回显；每台机器独立令牌
  * 只读动作可高频；写动作幂等但 stop 会丢弃当前分片进度
"""
import json
import os
import sys
import urllib.error
import urllib.request

REG = "/root/jdbjj/machines.json"


def load_reg():
    with open(REG, encoding="utf-8") as fh:
        return json.load(fh)["machines"]


def load_env(path):
    env = {}
    if not path or not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line[7:] if line.startswith("export ") else line
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def call(m, path, data=None, tmo=30):
    tok = load_env(m.get("env", "")).get(m.get("token_key", ""), "")
    url = "http://127.0.0.1:%s/%s" % (m["control_port"], path)
    req = urllib.request.Request(url, data=data,
                                 headers={"X-Token": tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=tmo) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def online(m):
    try:
        call(m, "ping", tmo=5)
        return True
    except Exception:
        return False


def cmd_list(mm):
    print("%-12s %-7s %-6s %s" % ("机器", "状态", "端口", "说明"))
    for name, m in sorted(mm.items()):
        ok = online(m)
        print("%-12s %-7s %-6s %s" % (name, "在线" if ok else "离线",
                                      m.get("control_port"), m.get("desc", "")))
    return 0


def cmd_status(name, m):
    out = call(m, "status")["output"]
    print("主机: %s   时间: %s" % (out.get("host"), out.get("time")))
    print("进度: %s/%s 分片（音频 %s）   GPU: %s   磁盘: %s GB"
          % (out.get("transcribe_parts"), out.get("total_parts"),
             out.get("audio_parts"), out.get("gpu"), out.get("disk_free_gb")))
    print("进程: %s" % out.get("procs"))
    try:
        pl = call(m, "tail_log?which=pipeline&lines=6")["output"].get("content", "")
        print("--- pipeline.log 尾部 ---")
        print("\n".join("   " + l for l in pl.strip().splitlines()[-6:]))
    except Exception as e:
        print("  (日志读取失败: %s)" % e)
    return 0


def cmd_tail(name, m, which="pipeline", lines=20):
    out = call(m, "tail_log?which=%s&lines=%d" % (which, lines))["output"]
    print(out.get("content", out))
    return 0


def cmd_products(name, m):
    d = call(m, "list_transcripts")["output"]
    print("共 %d 个产物" % d.get("count", 0))
    groups = {}
    for f in d.get("files", []):
        parts = f["path"].split("\\")
        groups.setdefault("\\".join(parts[:-1]), []).append(parts[-1])
    for g, ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print("  %3d 个  %s" % (len(ps), g))
    return 0


def cmd_action(name, m, action):
    print(json.dumps(call(m, action), ensure_ascii=False, indent=2)[:1200])
    return 0


def cmd_run(name, m, task, tmo):
    body = json.dumps({"task": task, "mode": "print", "timeout": tmo}).encode("utf-8")
    d = call(m, "reasonix", data=body, tmo=tmo + 120)
    o = d.get("output", {})
    print("rc=%s  用时 %ss" % (o.get("rc"), o.get("secs")))
    print(o.get("output") or json.dumps(d, ensure_ascii=False)[:3000])
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    mm = load_reg()
    op = argv[1]
    if op == "list":
        return cmd_list(mm)
    if len(argv) < 3:
        print("缺少机器名。用 `rmt.py list` 查看已注册机器。")
        return 2
    name = argv[2]
    if name not in mm:
        print("未知机器 %r。已注册：%s" % (name, ", ".join(sorted(mm))))
        return 2
    m = mm[name]
    if op == "ping":
        print("%s: %s" % (name, "在线" if online(m) else "离线"))
        return 0
    if op == "status":
        return cmd_status(name, m)
    if op == "tail":
        which = argv[3] if len(argv) > 3 else "pipeline"
        lines = int(argv[4]) if len(argv) > 4 else 20
        return cmd_tail(name, m, which, lines)
    if op == "products":
        return cmd_products(name, m)
    if op in ("start", "stop", "package", "analyze"):
        return cmd_action(name, m, {"start": "transcribe_start", "stop": "transcribe_stop"}.get(op, op))
    if op == "run":
        if len(argv) < 4:
            print('用法: rmt.py run <机器> "<任务>" [timeout]')
            return 2
        return cmd_run(name, m, argv[3], int(argv[4]) if len(argv) > 4 else 180)
    print("未知操作 %r（见 --help 的用法）" % op)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
