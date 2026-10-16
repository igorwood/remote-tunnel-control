#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""enroll.py —— 代理注册凭证管理 CLI（云端，凭证制注册，不依赖 SSH）

用法：
    python3 enroll.py issue --desc "第2台·渲染机" --control-port 9100 --serve-port 9120 \
                            --guard-base 19200 [--machine-id machine2] [--ttl 60]
    python3 enroll.py list                    # 凭证一览（状态/剩余有效期）
    python3 enroll.py verify <凭证>            # 校验某凭证是否可用
    python3 enroll.py machines                # 已注册机器（读 machines.json）
    python3 enroll.py revoke <machine_id>     # 注销机器（自动用 admin key）
    python3 enroll.py serve                   # 前台启动注册服务

设计要点：
  * 凭证一次性、默认 60 分钟 TTL、**绑定预留端口**（代理只能注册到分配好的端口）
  * issue 直接打印**可粘贴给代理的接入块**（含端口约定与回执要求）
"""
import argparse
import json
import os
import secrets
import sys
import time
import urllib.request

BASE = "/opt/remote-tunnel-control"
ENROLL = os.path.join(BASE, "enroll")
TICKETS = os.path.join(ENROLL, "tickets.json")
ADMIN_KEY_FILE = os.path.join(ENROLL, "admin.key")
REGISTRY = os.path.join(BASE, "machines.json")
SERVICE = "http://127.0.0.1:9099"
CLOUD_PUBLIC = "https://CLOUD_HOST/enroll"

SCRIPT_OFFSETS = [
    ("supervisor.py", 0), ("watchdog.py", 1), ("watchdog_transcribe.py", 2),
    ("watchdog_tunnel.py", 3), ("run_pipeline.py", 4), ("transcribe.py", 5),
    ("reverse_tunnel.py", 6), ("local_control_server.py", 7), ("agent_daemon.py", 8),
    ("monitor.py", 9), ("package_and_upload.py", 10),
]


def load_json(p, d):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return d


def save_json(p, o):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(o, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def new_ticket_str():
    raw = secrets.token_hex(16).upper()
    b32 = "".join(c for c in raw if c.isalnum())[:20]
    return "RGT-" + "-".join(b32[i:i + 5] for i in range(0, 20, 5))


def paste_block(tid, control, serve, guard, ttl):
    """生成可直接贴给代理的接入块（用 %%X%% 占位，避免与文本里的花括号冲突）。"""
    guard_rows = "\n".join("     %s = %d" % (s, guard + off) for s, off in SCRIPT_OFFSETS)
    tpl = """【任务】用注册凭证接入云端控制面（**不需要 SSH，也不需要拷贝任何令牌**）

■ 你的额度（云端已预留，照用不要改）
  云端隧道端口：control=%CONTROL% ／ agent=%SERVE%
  本机单实例守卫端口段：%GUARD%–%GUARD10%
  注册凭证（一次性、%TTL% 分钟内有效）：%TICKET%

■ 步骤
1) （可选）先自查凭证：
   python -c "import urllib.request;print(urllib.request.urlopen('https://CLOUD_HOST/enroll/verify?ticket=%TICKET%').read().decode())"
2) 建项目目录 + .venv，装 paramiko
3) 部署脚本（管理员提供）：reverse_tunnel.py、local_control_server.py、agent_daemon.py、
   supervisor.py、watchdog.py、watchdog_tunnel.py、_singleton.py、enroll_client.py
4) 给长驻脚本注入单实例守卫，端口按你的端口段顺延：
%GUARDROWS%
   插入模板（放在脚本的 if __name__ == "__main__": 之前）：
     try:
         import _singleton
         if not _singleton.acquire("<脚本名>", <上表端口>):
             raise SystemExit(0)
     except ImportError:
         pass
4.5) ★ 声明你的**运行时**（云端据此学会怎么调你；不绑定具体 agent）
   - 在项目根放 `runtime.json`（示例，按你的实际情况改命令名/参数）：
     { "default": "kimi",
       "kimi":   {"type": "cli_arg", "cmd": ["kimi", "--prompt"], "label": "Kimi"},
       "claude": {"type": "cli_arg", "cmd": ["claude", "-p"],     "label": "Claude Code"},
       "gpt":    {"type": "openai_http", "base_url": "https://api.openai.com/v1",
                  "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY", "label": "OpenAI"} }
   - 部署两个脚本（管理员提供）：`agent_adapters.py`（适配器层）、`agent_bridge.py`（任务桥）
   - 自测并生成「能力卡片」：
     python scripts\agent_bridge.py --self-test
   - 把卡片 JSON 随回执一并交给云端 → 云端 `agent_probe.py <machine>` 会**实地探测复核**
     （动作发现 + 工具能力 + 延迟基准），并写入该机画像；之后云端派活自动用对的动作与超时。
   ⚠️ 控制面若支持"任意任务"，请暴露**统一动作名 `/agent`**（兼容别名 /task、/run；旧名 /reasonix 也认）。
5) 兑换凭证换令牌（**这一步替代"拷贝 .agent_token"**）：
   python scripts\\enroll_client.py --ticket %TICKET% --hostname %COMPUTERNAME% ^
       --control-port %CONTROL% --serve-port %SERVE% --guard-base %GUARD% --desc "<用途>"
   → 成功后 token 会自动写入 scripts\\.agent_token（权限 600），并返回 machine_id
6) 起本地服务：
   python scripts\\local_control_server.py --port %CONTROL%
   reasonix-cli.exe serve --addr 127.0.0.1:%SERVE% --auth token
7) 起隧道（左侧必须是云端分配的 %CONTROL%/%SERVE%）：
   .venv\\Scripts\\python.exe scripts\\reverse_tunnel.py --map %CONTROL%:%CONTROL% --map %SERVE%:%SERVE%
8) 用 CIM 起 supervisor（不要用会话内子进程）：
   Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='"%PY%" "%ROOT%\\scripts\\supervisor.py" --interval 300'; CurrentDirectory='%ROOT%'}
9) 回执 5 项（**不要贴令牌明文**）：主机名／本地端口／machine_id／/ping 原文／守卫是否注入

■ 安全红线
   隧道端口只绑 127.0.0.1；凭证一次性、勿转发；令牌只落在本机 scripts\\.agent_token
"""
    rep = {"%TICKET%": tid, "%CONTROL%": str(control), "%SERVE%": str(serve),
           "%GUARD%": str(guard), "%GUARD10%": str(guard + 10), "%TTL%": str(ttl),
           "%GUARDROWS%": guard_rows, "%PY%": "<venv\\Scripts\\python.exe>", "%ROOT%": "<项目根>"}
    for k, v in rep.items():
        tpl = tpl.replace(k, v)
    return tpl


def cmd_issue(a):
    tid = new_ticket_str()
    rec = {"id": tid, "desc": a.desc or "未命名", "machine_id": a.machine_id,
           "control_port": a.control_port, "serve_port": a.serve_port,
           "guard_base": a.guard_base, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
           "expires_ts": time.time() + a.ttl * 60, "ttl_min": a.ttl, "status": "open"}
    t = load_json(TICKETS, {"tickets": {}})
    t.setdefault("tickets", {})[tid] = rec
    save_json(TICKETS, t)
    print("✅ 已签发注册凭证（一次性 / %d 分钟有效）" % a.ttl)
    print("   凭证   : %s" % tid)
    print("   预留   : control_port=%s  serve_port=%s  guard_base=%s"
          % (a.control_port, a.serve_port, a.guard_base))
    print("   说明   : %s" % rec["desc"])
    print("   过期   : %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["expires_ts"])))
    print("\n" + "=" * 72)
    print("↓ 把下面整段贴给待接入的代理 ↓")
    print("=" * 72)
    print(paste_block(tid, a.control_port, a.serve_port, a.guard_base, a.ttl))
    print("=" * 72)
    return 0


def cmd_list(a):
    t = load_json(TICKETS, {"tickets": {}}).get("tickets", {})
    if not t:
        print("(无凭证)")
        return 0
    print("%-28s %-8s %-8s %-18s %s" % ("凭证", "状态", "剩余", "预留端口", "说明"))
    for tid, r in sorted(t.items(), key=lambda kv: kv[1].get("created", ""), reverse=True):
        left = int((r.get("expires_ts") or 0) - time.time())
        st = r.get("status", "open")
        if st == "open" and left <= 0:
            st = "expired"
        print("%-28s %-8s %-8s c=%-6s s=%-6s %s" % (tid, st,
              ("%d分" % (left // 60)) if left > 0 else "过期",
              r.get("control_port"), r.get("serve_port"), (r.get("desc") or "")[:24]))
    return 0


def cmd_verify(a):
    try:
        with urllib.request.urlopen(SERVICE + "/verify?ticket=" + a.ticket, timeout=10) as r:
            print(r.read().decode("utf-8", "ignore"))
        return 0
    except Exception as e:
        rec = load_json(TICKETS, {"tickets": {}}).get("tickets", {}).get(a.ticket)
        print("(服务未启动，离线校验) %s" % ("凭证存在" if rec else "凭证不存在"))
        if rec:
            print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0


def cmd_machines(a):
    reg = load_json(REGISTRY, {"machines": {}})
    print("%-12s %-34s %-8s %-8s %s" % ("机器", "说明", "控制面", "agent", "注册方式"))
    for k, m in reg.get("machines", {}).items():
        print("%-12s %-34s %-8s %-8s %s" % (k, (m.get("desc") or "")[:32],
              m.get("control_port"), m.get("serve_port"), m.get("method", "-")))
    return 0


def cmd_revoke(a):
    key = ""
    try:
        key = open(ADMIN_KEY_FILE, encoding="utf-8").read().strip()
    except Exception:
        pass
    body = json.dumps({"admin_key": key, "machine_id": a.machine_id}).encode()
    req = urllib.request.Request(SERVICE + "/revoke", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        print("服务不可用：%s" % e)
    return 0


def cmd_serve(a):
    from enroll_service import main as svc
    svc()
    return 0


def main():
    ap = argparse.ArgumentParser(description="代理注册凭证管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("issue")
    p.add_argument("--desc", default="")
    p.add_argument("--machine-id", default="")
    p.add_argument("--control-port", type=int, required=True)
    p.add_argument("--serve-port", type=int, required=True)
    p.add_argument("--guard-base", type=int, default=0)
    p.add_argument("--ttl", type=int, default=60)
    p.set_defaults(fn=cmd_issue)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    q = sub.add_parser("verify")
    q.add_argument("ticket")
    q.set_defaults(fn=cmd_verify)
    sub.add_parser("machines").set_defaults(fn=cmd_machines)
    r = sub.add_parser("revoke")
    r.add_argument("machine_id")
    r.set_defaults(fn=cmd_revoke)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
