#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 nginx 443 站点加 /enroll 反代（指向 127.0.0.1:9099 注册服务）。

安全：① 先备份 ② 断言锚点唯一、且无同名 location ③ 改后 nginx -t 不通过就自动回滚 ④ 通过才 reload
产物：/opt/remote-tunnel-control/backup/<file>.<ts>.bak
"""
import os
import shutil
import subprocess
import sys
import time

CONF = "/etc/nginx/sites-available/geoplatform"
BACKUP_DIR = "/opt/remote-tunnel-control/backup"
ANCHOR = "\n    location / {"
BLOCK = """
    # ==== 代理注册服务（凭证制，不依赖 SSH）；由 enroll_service.py 提供 ====
    location = /enroll {
        proxy_pass http://127.0.0.1:9099/enroll;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 64k;
        proxy_read_timeout 30s;
    }
    location ^~ /enroll/ {
        proxy_pass http://127.0.0.1:9099/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 64k;
        proxy_read_timeout 30s;
    }
"""


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def main():
    src = open(CONF, encoding="utf-8").read()
    if "/enroll" in src or "9099" in src:
        print("[SKIP] 配置里已有 /enroll 或 9099")
        return 0
    cnt = src.count(ANCHOR)
    if cnt != 1:
        print("[ABORT] 锚点 %r 出现 %d 次（期望 1），不自动改配置" % (ANCHOR.strip(), cnt))
        return 1
    # 确认锚点在 443 块内（其后不应再有 listen 443）
    idx = src.index(ANCHOR)
    tail = src[idx:]
    if "listen 443" in tail:
        print("[ABORT] 锚点之后还有别的 443 server 块，位置不安全")
        return 1

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(BACKUP_DIR, "geoplatform.nginx.%s.bak" % ts)
    shutil.copy2(CONF, bak)
    print("[1/4] 已备份 → %s（%d 字节）" % (bak, os.path.getsize(bak)))

    new = src.replace(ANCHOR, BLOCK + ANCHOR, 1)
    with open(CONF, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
    print("[2/4] 已插入 /enroll 反代（+%d 字节）" % (len(new) - len(src)))

    rc, out = run(["nginx", "-t"])
    print("[3/4] nginx -t → rc=%s\n%s" % (rc, out))
    if rc != 0:
        shutil.copy2(bak, CONF)
        print("[ROLLBACK] 配置校验失败，已回滚；nginx 未重载")
        return 1

    rc, out = run(["systemctl", "reload", "nginx"])
    print("[4/4] reload → rc=%s %s" % (rc, out or "OK"))
    rc, out = run(["systemctl", "is-active", "nginx"])
    print("      nginx 状态:", out)
    print("\n生效路径：")
    print("  POST https://CLOUD_HOST/enroll          → 127.0.0.1:9099/enroll")
    print("  GET  https://CLOUD_HOST/enroll/health   → 127.0.0.1:9099/health")
    print("  GET  https://CLOUD_HOST/enroll/verify?ticket=… → 127.0.0.1:9099/verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
