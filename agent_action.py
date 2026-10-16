#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""agent_action.py —— 【参考实现】让你的控制面支持统一的「任意任务」动作

云端只认一个契约（动作名优先 /agent，兼容 /task、/run、旧名 /reasonix）：

    POST /agent
    {"task": "自然语言任务", "timeout": 180, "runtime": "kimi"（可选）}
    → {"ok": true, "output": {"rc": 0, "output": "…", "runtime": "Kimi",
                              "adapter": "cli_arg", "secs": 1.2}}

两种接入方式
────────────
① 在你的控制面里 import（推荐，最小改动）：

      import agent_action
      ...
      if action in ("agent", "task", "run", "reasonix"):
          return agent_action.handle_action(payload)      # payload 是请求体 dict

② 独立起一个小服务（不改动控制面时）：

      python scripts\\agent_action.py --serve --port 9090 [--token-file scripts\\.agent_token]
      # 然后把隧道映射到这个端口即可：--map <云端端口>:9090

依赖：agent_bridge.py、agent_adapters.py（同目录）
"""
import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ACTION_NAMES = ("agent", "task", "run", "reasonix")
TOKEN = None          # 若设置，则要求 X-Token 头匹配


def handle_action(payload):
    """控制面调用入口：把云端任务交给本机运行时执行。"""
    import agent_bridge
    req = dict(payload or {})
    res = agent_bridge.handle(req)                 # {rc, output, runtime, adapter, secs}
    return {"ok": res.get("rc") != 501, "output": res,
            "action": "agent", "bridge": "agent_action.py/1.0"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                                  # noqa: N802
        if self.path == "/ping":
            return self._send(200, {"ok": True, "output": {"pong": True, "ver": "agent_action/1.0"}})
        if self.path == "/status":
            import platform
            return self._send(200, {"ok": True, "output": {
                "host": platform.node(), "platform": platform.platform(),
                "actions": list(ACTION_NAMES), "bridge": "agent_action/1.0"}})
        return self._send(404, {"ok": False, "error": "未知端点"})

    def do_POST(self):                                                 # noqa: N802
        action = self.path.strip("/").split("/")[0]
        if action not in ACTION_NAMES:
            return self._send(404, {"ok": False, "error": "未知端点",
                                    "hint": "支持 %s" % ", ".join("/" + a for a in ACTION_NAMES)})
        if TOKEN and self.headers.get("X-Token") != TOKEN:
            return self._send(401, {"ok": False, "error": "令牌不匹配"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception as e:                                          # noqa: BLE001
            return self._send(400, {"ok": False, "error": "请求体解析失败: %s" % e})
        try:
            return self._send(200, handle_action(payload))
        except Exception as e:                                          # noqa: BLE001
            return self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def log_message(self, fmt, *args):                                  # 静音
        pass


def serve(port, token=None, host="127.0.0.1"):
    global TOKEN
    TOKEN = token
    srv = ThreadingHTTPServer((host, port), Handler)
    print("agent_action 已启动: http://%s:%s  动作 %s  令牌校验 %s"
          % (host, port, "/".join(ACTION_NAMES), "开" if token else "关"))
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=9090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--task", default=None, help="不启服务，直接跑一条任务（自测）")
    ap.add_argument("--runtime", default=None)
    a = ap.parse_args()
    tok = None
    if a.token_file and os.path.exists(a.token_file):
        tok = open(a.token_file, encoding="utf-8").read().strip()
    if a.task:
        print(json.dumps(handle_action({"task": a.task, "runtime": a.runtime}), ensure_ascii=False))
        return 0
    if a.serve:
        serve(a.port, tok, a.host)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
