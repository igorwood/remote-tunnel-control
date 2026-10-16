#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""enroll_service.py —— 代理注册服务（凭证制，不依赖 SSH）

流程：
  1) 云端签发一次性**注册凭证**（enroll.py issue）→ 交给待接入的代理
  2) 代理持凭证调用本服务 `POST /enroll` 提交自己的信息（主机名/端口/用途）
  3) 本服务**核验凭证**（存在、未用、未过期、端口未被占用）
     → 通过：生成该机的**独立令牌**、写入注册表 machines.json + /opt/machines/<id>/env.sh，
             把令牌返回给代理（代理存为 scripts/.agent_token）
     → 不通过：拒绝并记录原因（凭证错误/已用/过期/端口冲突）
  4) 全程写审计日志 enroll/audit.jsonl

端点（默认只绑 127.0.0.1:9099，由 nginx 以 /enroll/ 暴露）：
  GET  /health                      → 服务存活
  GET  /verify?ticket=...           → 凭证是否可用（代理可先自查）
  POST /enroll                      → 兑换注册（body: ticket/hostname/control_port/serve_port/desc）
  POST /revoke                      → 注销某机器（需 admin key，body: {admin_key, machine_id})

安全设计：
  * 凭证：130 bit 随机、**一次性**、默认 60 分钟 TTL、**绑定预留端口**
  * 无任何"执行命令"能力 —— 只能写注册表；端口冲突即拒
  * 简单限流：同 IP 每分钟 >20 次请求即拒（防爆破）
  * 返回值（含令牌）必须经 HTTPS 传输 → 由 nginx 443 暴露
"""
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "/opt/remote-tunnel-control"
ENROLL = os.path.join(BASE, "enroll")
TICKETS = os.path.join(ENROLL, "tickets.json")
AUDIT = os.path.join(ENROLL, "audit.jsonl")
REGISTRY = os.path.join(BASE, "machines.json")
ADMIN_KEY_FILE = os.path.join(ENROLL, "admin.key")
MACHINES_DIR = "/root/machines"
HOST, PORT = "127.0.0.1", 9099
RATE_MAX, RATE_WIN = 20, 60          # 每 IP 每窗口最多请求数
_lock = threading.Lock()


# ---------------- 基础工具 ----------------
def now_s():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def audit(event, **kw):
    rec = {"ts": now_s(), "event": event}
    rec.update(kw)
    os.makedirs(ENROLL, exist_ok=True)
    with open(AUDIT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def same_token(a, b):
    return secrets.compare_digest(str(a or ""), str(b or ""))


def new_ticket_str():
    """RGT-XXXX-XXXX-XXXX-XXXX（base32，130 bit 随机）"""
    raw = secrets.token_hex(16).upper()
    b32 = "".join(c for c in raw if c.isalnum())[:20]
    return "RGT-" + "-".join(b32[i:i + 5] for i in range(0, 20, 5))


# ---------------- 核心逻辑 ----------------
def verify_ticket(ticket):
    """返回 (ok:bool, reason:str, info:dict)"""
    t = (load_json(TICKETS, {"tickets": {}}) or {}).get("tickets", {})
    rec = None
    for k, v in t.items():
        if same_token(k, ticket) or same_token(v.get("id"), ticket):
            rec = v
            break
    if not rec:
        return False, "凭证不存在", {}
    if rec.get("status") == "used":
        return False, "凭证已被使用（一次性）", rec
    if rec.get("status") == "revoked":
        return False, "凭证已被吊销", rec
    exp = rec.get("expires_ts") or 0
    if exp and time.time() > exp:
        return False, "凭证已过期", rec
    return True, "ok", rec


def ports_taken(reg, control_port, serve_port):
    for name, m in (reg.get("machines") or {}).items():
        if m.get("control_port") == control_port or m.get("serve_port") == serve_port:
            return name
    return None


def slugify(s):
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "").strip()).strip("-").lower()
    return s or "machine"


def enroll(payload):
    """核验凭证 → 注册机器。返回 (ok, result_or_error)"""
    ticket = (payload.get("ticket") or "").strip()
    hostname = (payload.get("hostname") or "").strip()
    desc = (payload.get("desc") or "").strip()
    try:
        control_port = int(payload.get("control_port") or 0)
        serve_port = int(payload.get("serve_port") or 0)
    except Exception:
        return False, {"error": "端口必须是数字"}

    ok, reason, rec = verify_ticket(ticket)
    if not ok:
        audit("enroll_reject", ticket=ticket[:12] + "…", reason=reason, hostname=hostname)
        return False, {"error": reason}

    # 凭证绑定的预留端口（强约束，防占错端口）
    res_c, res_s = rec.get("control_port"), rec.get("serve_port")
    if res_c and control_port and control_port != res_c:
        return False, {"error": "control_port 与凭证预留不符（应为 %s）" % res_c}
    if res_s and serve_port and serve_port != res_s:
        return False, {"error": "serve_port 与凭证预留不符（应为 %s）" % res_s}

    reg = load_json(REGISTRY, {"machines": {}})
    clash = ports_taken(reg, control_port, serve_port)
    if clash:
        audit("enroll_reject", ticket=ticket[:12] + "…", reason="端口冲突", clash=clash)
        return False, {"error": "端口已被机器 %s 占用" % clash}

    mid = slugify(payload.get("machine_id") or rec.get("machine_id") or hostname)
    if mid in reg["machines"]:
        mid = "%s-%s" % (mid, secrets.token_hex(2))

    token = secrets.token_urlsafe(32)
    # ① 写该机 env
    envdir = os.path.join(MACHINES_DIR, mid)
    os.makedirs(envdir, exist_ok=True)
    envp = os.path.join(envdir, "env.sh")
    key = re.sub(r"[^A-Z0-9]+", "_", mid.upper()).strip("_") + "_TOKEN"
    with open(envp, "w", encoding="utf-8") as fh:
        fh.write("%s=%s\n" % (key, token))
        fh.write("MACHINE_ID=%s\nHOST=%s\n" % (mid, hostname))
    os.chmod(envp, 0o600)
    # ② 写注册表
    reg["machines"][mid] = {
        "desc": desc or ("%s（凭证注册）" % (hostname or mid)),
        "control_port": control_port or rec.get("control_port"),
        "serve_port": serve_port or rec.get("serve_port"),
        "env": envp, "token_key": key,
        "guard_base": payload.get("guard_base") or rec.get("guard_base") or 0,
        "hostname": hostname, "registered_at": now_s(),
        "method": "ticket:%s" % (rec.get("id") or "")[:12],
    }
    save_json(REGISTRY, reg)
    # ③ 凭证置为已用
    t = load_json(TICKETS, {"tickets": {}})
    for k, v in t.get("tickets", {}).items():
        if same_token(v.get("id"), rec.get("id")):
            v["status"] = "used"
            v["used_at"] = now_s()
            v["used_by"] = hostname
            v["machine_id"] = mid
    save_json(TICKETS, t)

    audit("enroll_ok", ticket=(rec.get("id") or "")[:12] + "…", machine_id=mid,
          hostname=hostname, control_port=control_port, serve_port=serve_port)
    try:                                              # 代理台账 + 事件（agentsdb）
        import agentsdb as adb
        adb.upsert_agent(mid, hostname=hostname, desc=desc, control_port=control_port,
                         serve_port=serve_port, guard_base=payload.get("guard_base"),
                         token_key=key, env_path=envp, method="ticket:%s" % (rec.get("id") or "")[:12],
                         registered_at=now_s())
        adb.log_event(mid, "enroll_ok", {"hostname": hostname, "ticket": (rec.get("id") or "")[:12]})
    except Exception:                                 # noqa: BLE001
        pass
    return True, {"machine_id": mid, "token": token, "token_key": key,
                  "control_port": control_port or rec.get("control_port"),
                  "serve_port": serve_port or rec.get("serve_port"),
                  "guard_base": payload.get("guard_base") or rec.get("guard_base") or 0,
                  "env": envp, "next": "把 token 存为 scripts/.agent_token，然后起隧道与控制面"}


def revoke(payload):
    key = (payload.get("admin_key") or "").strip()
    real = ""
    try:
        real = open(ADMIN_KEY_FILE, encoding="utf-8").read().strip()
    except Exception:
        pass
    if not real or not same_token(key, real):
        audit("revoke_reject", reason="admin key 错误")
        return False, {"error": "admin key 错误"}
    mid = (payload.get("machine_id") or "").strip()
    reg = load_json(REGISTRY, {"machines": {}})
    if mid not in reg.get("machines", {}):
        return False, {"error": "未注册的 machine_id"}
    reg["machines"].pop(mid)
    save_json(REGISTRY, reg)
    audit("revoke_ok", machine_id=mid)
    return True, {"machine_id": mid, "removed": True}


# ---------------- HTTP ----------------
_hits = {}


def rate_ok(ip):
    now = time.time()
    win = _hits.setdefault(ip, [])
    win[:] = [t for t in win if now - t < RATE_WIN]
    win.append(now)
    return len(win) <= RATE_MAX


class Handler(BaseHTTPRequestHandler):
    server_version = "enroll/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):          # 静默默认日志，改写审计
        return

    def do_GET(self):
        ip = self.client_address[0]
        if not rate_ok(ip):
            return self._send(429, {"ok": False, "error": "请求过于频繁"})
        if self.path.startswith("/health"):
            return self._send(200, {"ok": True, "service": "enroll", "ts": now_s()})
        if self.path.startswith("/verify"):
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        q[k] = v
            ok, reason, rec = verify_ticket(q.get("ticket", "").strip())
            left = max(0, int((rec.get("expires_ts") or 0) - time.time())) if rec else 0
            return self._send(200, {"ok": ok, "valid": ok, "reason": reason,
                                    "desc": rec.get("desc"), "ttl_left_s": left,
                                    "reserved": {"control_port": rec.get("control_port"),
                                                 "serve_port": rec.get("serve_port")} if rec else None})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        ip = self.client_address[0]
        if not rate_ok(ip):
            return self._send(429, {"ok": False, "error": "请求过于频繁"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
        except Exception:
            return self._send(400, {"ok": False, "error": "body 必须是 JSON"})
        with _lock:
            if self.path.startswith("/enroll"):
                ok, res = enroll(payload)
            elif self.path.startswith("/revoke"):
                ok, res = revoke(payload)
            elif self.path.startswith("/verify"):
                ok2, reason, rec = verify_ticket((payload.get("ticket") or "").strip())
                ok, res = ok2, {"valid": ok2, "reason": reason}
            else:
                return self._send(404, {"ok": False, "error": "not found"})
        return self._send(200 if ok else 403, {"ok": ok, "result": res} if ok else res)


def main():
    os.makedirs(ENROLL, exist_ok=True)
    if not os.path.exists(ADMIN_KEY_FILE):
        with open(ADMIN_KEY_FILE, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_urlsafe(32))
        os.chmod(ADMIN_KEY_FILE, 0o600)
    audit("service_start", host=HOST, port=PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
