#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查两件事：① GitHub 推送能力（只做 dry-run，不改远端）② 与各代理机器的联系是否正常。"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO_DIR = "/root/repos/remote-tunnel-control"
KEY = os.path.expanduser("~/.ssh/id_ed25519_github")
BASE = "/opt/remote-tunnel-control"
SEP = "=" * 74


def run(cmd, cwd=None, timeout=60, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd, env=env or os.environ.copy())
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "超时"


print(SEP)
print("① GitHub 推送能力检查（dry-run，不动远端）")
print(SEP)

# 1.1 令牌身份
tok = ""
try:
    tok = open("/root/.github_token", encoding="utf-8").read().strip()
except Exception as e:
    print("  令牌文件读取失败: %s" % e)
if tok:
    try:
        req = urllib.request.Request("https://api.github.com/user",
                                     headers={"Authorization": "Bearer " + tok,
                                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            u = json.loads(r.read().decode())
        print("  · 令牌身份: %s（%s）" % (u.get("login"), u.get("type")))
    except Exception as e:
        print("  · 令牌查询失败: %s: %s" % (type(e).__name__, e))
    # 1.2 仓库权限
    try:
        req = urllib.request.Request("https://api.github.com/repos/igorwood/remote-tunnel-control",
                                     headers={"Authorization": "Bearer " + tok,
                                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode())
        perms = d.get("permissions", {})
        print("  · 仓库: %s ｜ 默认分支 %s ｜ 权限 push=%s admin=%s"
              % (d.get("full_name"), d.get("default_branch"), perms.get("push"), perms.get("admin")))
        print("    （注意：令牌权限为在【本机】执行 git push 时并不生效——推送走 SSH 部署密钥）")
    except Exception as e:
        print("  · 仓库查询失败: %s: %s" % (type(e).__name__, e))

# 1.3 本地仓库状态
if os.path.isdir(REPO_DIR):
    for cmd in (["git", "-C", REPO_DIR, "remote", "-v"],
                ["git", "-C", REPO_DIR, "log", "--oneline", "-3"],
                ["git", "-C", REPO_DIR, "status", "--short", "--branch"]):
        rc, out = run(cmd)
        print("  · %s → rc=%s\n    %s" % (" ".join(cmd[3:]), rc, out.replace("\n", "\n    ")[:400]))
else:
    print("  · 本地仓库不存在: %s" % REPO_DIR)

# 1.4 SSH 部署密钥认证（写成子进程，不用交互命令）
if os.path.exists(KEY):
    rc, out = run(["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no",
                   "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                   "-p", "443", "git@ssh.github.com"], timeout=45)
    print("  · 部署密钥 SSH 认证（443 端口）: rc=%s → %s" % (rc, out[:200]))
else:
    print("  · 找不到部署密钥 %s" % KEY)

# 1.5 真正验证"能不能推"：dry-run（不改远端）
if os.path.isdir(REPO_DIR):
    rc, out = run(["git", "-C", REPO_DIR, "-c", "core.sshCommand=ssh -i %s -o StrictHostKeyChecking=no"
                   % KEY, "push", "--dry-run", "origin", "HEAD"], timeout=90)
    print("  · push --dry-run → rc=%s\n    %s" % (rc, out[:500]))
    if rc == 0 and ("up-to-date" in out.lower() or "Everything" in out or "would be" in out.lower()):
        print("    ✅ 结论: 推送通道可用（认证通过，dry-run 无报错）")
    elif rc != 0:
        print("    ⚠️ 结论: 推送受阻，见上（认证/权限/网络）")
else:
    print("  · 跳过 push dry-run（仓库不存在）")

print()
print(SEP)
print("② 与各代理机器的联系状况")
print(SEP)

# 2.1 注册服务
try:
    with urllib.request.urlopen("http://127.0.0.1:9099/health", timeout=10) as r:
        print("  · 注册服务 9099: %s" % r.read().decode()[:100])
except Exception as e:
    print("  · 注册服务 9099: ✗ %s" % e)
rc, out = run(["systemctl", "is-active", "enroll"])
print("  · systemd enroll.service: %s" % out)

# 2.2 隧道端口
rc, out = run(["ss", "-ltn"])
ports = {p: any((":%s " % p) in l for l in out.splitlines()) for p in
         (9000, 9020, 9100, 9120, 9099)}
print("  · 隧道端口: %s" % " ｜ ".join("%s=%s" % (p, "监听✓" if v else "未监听✗")
                                       for p, v in ports.items()))

# 2.3 逐机 ping/status
sys.path.insert(0, BASE)
reg = json.load(open(os.path.join(BASE, "machines.json"), encoding="utf-8"))["machines"]


def load_env(path):
    env = {}
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            line = line[7:] if line.startswith("export ") else line
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


for mid, m in reg.items():
    tok = load_env(m.get("env")).get(m.get("token_key", ""), "")
    url = "http://127.0.0.1:%s/ping" % m["control_port"]
    try:
        req = urllib.request.Request(url, headers={"X-Token": tok})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        print("  · %-9s /ping → %s（运行时 %s）" % (mid, json.dumps(d.get("output"), ensure_ascii=False)[:80],
                                                    m.get("runtime")))
    except Exception as e:
        print("  · %-9s /ping → ✗ %s: %s" % (mid, type(e).__name__, e))
    try:
        req = urllib.request.Request("http://127.0.0.1:%s/status" % m["control_port"],
                                     headers={"X-Token": tok})
        with urllib.request.urlopen(req, timeout=25) as r:
            st = json.loads(r.read().decode()).get("output", {})
        print("            /status → 主机 %s ｜ 时间 %s ｜ 磁盘 %s GB"
              % (st.get("host"), st.get("time"), st.get("disk_free_gb") or st.get("disk")))
    except Exception as e:
        print("            /status → ✗ %s: %s" % (type(e).__name__, e))

# 2.4 台账里的画像
try:
    import agentsdb as adb
    print()
    adb.show_stats()
    print()
    print("  画像:")
    agent_probe = __import__("agent_probe")
    agent_probe.show()
except Exception as e:
    print("  （画像读取失败: %s）" % e)
