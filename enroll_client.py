#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""enroll_client.py —— 在【待接入的代理机器】上运行：用凭证兑换注册令牌。

用法（在项目根目录）：
    python scripts\\enroll_client.py --ticket RGT-XXXXX-XXXXX-XXXXX-XXXXX \
        --hostname %COMPUTERNAME% --control-port 9100 --serve-port 9120 --desc "第2台·渲染机"

做的事：
  1) 调云端 `POST /enroll`（默认 https://CLOUD_HOST/enroll）核验凭证
  2) 成功后把返回的 token **写入 scripts/.agent_token**（权限 600），并打印 machine_id
  3) 失败则打印明确原因（凭证错误/已用/过期/端口冲突）

可选：
    --cloud https://<host>/enroll     指定端点（默认走 nginx 443）
    --dry-run                         只校验不写入
    --out scripts/.agent_token        指定令牌落盘位置
"""
import argparse
import json
import os
import secrets
import stat
import sys
import urllib.error
import urllib.request

DEFAULT_CLOUD = "https://CLOUD_HOST/enroll"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticket", required=True)
    ap.add_argument("--hostname", default=os.environ.get("COMPUTERNAME") or os.uname().nodename
                    if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "unknown"))
    ap.add_argument("--control-port", type=int, required=True)
    ap.add_argument("--serve-port", type=int, required=True)
    ap.add_argument("--guard-base", type=int, default=0)
    ap.add_argument("--desc", default="")
    ap.add_argument("--machine-id", default="")
    ap.add_argument("--cloud", default=DEFAULT_CLOUD)
    ap.add_argument("--out", default=os.path.join("scripts", ".agent_token"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    payload = {"ticket": a.ticket.strip(), "hostname": a.hostname,
               "control_port": a.control_port, "serve_port": a.serve_port,
               "guard_base": a.guard_base, "desc": a.desc, "machine_id": a.machine_id}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(a.cloud, data=body,
                                 headers={"Content-Type": "application/json"})
    print("→ 兑换凭证: %s（%s）" % (a.cloud, a.hostname))
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            res = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        print("✗ 注册被拒（HTTP %s）：%s" % (e.code, raw[:300]))
        return 1
    except Exception as e:
        print("✗ 无法连接云端注册服务：%s" % e)
        print("  检查：网络是否可达、凭证 URL 是否正确、是否被安全策略拦截")
        return 1

    if not res.get("ok"):
        print("✗ 注册失败：%s" % json.dumps(res, ensure_ascii=False))
        return 1

    out = res["result"]
    print("✓ 注册成功")
    print("   machine_id : %s" % out["machine_id"])
    print("   control_port=%s  serve_port=%s  guard_base=%s"
          % (out["control_port"], out["serve_port"], out.get("guard_base")))
    print("   令牌变量名 : %s（值已隐藏）" % out.get("token_key"))
    if a.dry_run:
        print("   --dry-run：未写入令牌文件")
        return 0

    path = a.out
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out["token"] + "\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    print("   令牌已写入 %s（权限 600）" % path)
    print("   下一步：起控制面与隧道 ——")
    print("     python scripts\\local_control_server.py --port %s" % out["control_port"])
    print("     .venv\\Scripts\\python.exe scripts\\reverse_tunnel.py --map %s:%s --map %s:%s"
          % (out["serve_port"], out["serve_port"], out["control_port"], out["control_port"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
