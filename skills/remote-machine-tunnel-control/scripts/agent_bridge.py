#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""agent_bridge.py —— 【跑在代理机器上】的通用任务桥

作用：让**任何运行时**（Kimi / GLM / Claude / GPT / Gemini / 自研 CLI / HTTP API）
都能被云端以同一个动作调用。两种用法：

  ① 命令行（人测/脚本调用）：
       python scripts\\agent_bridge.py --task "只回复 OK"
       python scripts\\agent_bridge.py --runtime-file runtime.json --runtime kimi --task "..."
     → 打印 JSON: {"rc":0,"output":"OK","runtime":"Kimi","adapter":"cli_arg","secs":1.2}

  ② 被控制面 import（推荐）：把控制面的"任意任务"动作转交本模块
       import agent_bridge
       res = agent_bridge.handle({"task": task, "runtime": "kimi", "timeout": 180})
       → 与上面同样的 JSON（控制面把它原样返回给云端）

运行时配置文件 runtime.json（放在项目根或 scripts/ 下，示例）：
    {
      "default": "kimi",
      "kimi":   {"type": "cli_arg", "cmd": ["kimi", "--prompt"], "label": "Kimi"},
      "claude": {"type": "cli_arg", "cmd": ["claude", "-p"],     "label": "Claude Code"},
      "glm":    {"type": "cli_stdin", "cmd": ["glm", "chat"],     "label": "智谱 GLM"},
      "gpt":    {"type": "openai_http", "base_url": "https://api.openai.com/v1",
                 "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY", "label": "OpenAI"},
      "reasonix": {"type": "cli_arg", "cmd": ["reasonix", "-p"],  "label": "reasonix"}
    }

约定（云端据此判断能力）：
  * 成功 → rc=0；命令不存在 → rc=127；超时 → rc=124；未启用 → rc=501
  * 云端注册表里为每台机器记 `runtime`（默认运行时名）与 `agent_action`
    （控制面暴露的动作名，建议统一叫 "agent"，兼容旧的 "reasonix"）
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent_adapters as aa  # noqa: E402

RUNTIME_FILES = [os.path.join(HERE, "runtime.json"),
                 os.path.join(os.path.dirname(HERE), "runtime.json")]


def find_runtime_file():
    for p in RUNTIME_FILES:
        if os.path.exists(p):
            return p
    return None


def handle(req):
    """统一入口：req = {task, runtime?, timeout?, spec?} → 归一结果 dict"""
    task = (req.get("task") or "").strip()
    if not task:
        return {"rc": 2, "output": "缺少 task", "runtime": None, "adapter": None, "secs": 0.0}
    timeout = int(req.get("timeout") or 180)
    rf = req.get("runtime_file") or find_runtime_file()
    kind = req.get("runtime")
    cfg = None
    if rf:
        try:
            with open(rf, encoding="utf-8") as fh:
                all_cfg = json.load(fh)
            kind = kind or all_cfg.get("default")
            if kind and isinstance(all_cfg.get(kind), dict):
                cfg = dict(all_cfg[kind])
                cfg.setdefault("label", kind)
            elif "type" in all_cfg:                    # 单套配置（没有分层）
                cfg = dict(all_cfg)
        except Exception as e:                         # noqa: BLE001
            return {"rc": 3, "output": "runtime 配置读取失败: %s" % e,
                    "runtime": kind, "adapter": None, "secs": 0.0}
    if cfg is None:
        cfg = aa.load_runtime(spec=req.get("spec"), kind=kind or "none")
    if kind and not cfg.get("label"):
        cfg["label"] = kind
    res = aa.run_task(cfg, task, timeout)
    res["runtime_file"] = rf
    return res


def self_test(runtime=None, timeout=60):
    """能力卡片：让云端知道"我是谁、怎么调我、我能干什么"。
    代理接入时把这份 JSON 交给云端（agent_probe.py --self-report），云端再做一次实地探测复核。"""
    import platform
    rf = find_runtime_file()
    cfg = None
    kind = runtime
    if rf:
        try:
            with open(rf, encoding="utf-8") as fh:
                all_cfg = json.load(fh)
            kind = kind or all_cfg.get("default")
            if kind and isinstance(all_cfg.get(kind), dict):
                cfg = dict(all_cfg[kind])
        except Exception:
            cfg = None
    cfg = cfg or aa.load_runtime(kind=kind or "none")
    probe = handle({"task": "只回复 OK 两个字母", "runtime": kind, "timeout": timeout})
    tools = {}
    for cmd in ("python", "python3", "node", "npm", "git", "docker", "pwsh", "bash", "sh"):
        import shutil
        tools[cmd] = bool(shutil.which(cmd))
    return {
        "bridge": "agent_bridge.py/1.0",
        "runtime": cfg.get("label") or kind or "none",
        "adapter": cfg.get("type"),
        "runtime_file": rf,
        "invocation": {k: cfg.get(k) for k in ("type", "cmd", "cmd_tpl", "base_url", "model")
                       if cfg.get(k) is not None},
        "platform": {"system": platform.system(), "release": platform.release(),
                     "python": platform.python_version(), "machine": platform.machine()},
        "tools_available": {k: v for k, v in tools.items() if v},
        "self_test": {"rc": probe.get("rc"), "secs": probe.get("secs"),
                      "output": (probe.get("output") or "")[:200]},
        "capabilities": {"task_channel": probe.get("rc") == 0,
                         "tools": None,          # 由云端探测（P2_tools）确认
                         "session_resume": bool(cfg.get("session_resume"))},
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", help="要执行的任务（与 --self-test 二选一）")
    ap.add_argument("--self-test", action="store_true", help="打印能力卡片（交给云端）")
    ap.add_argument("--runtime", default=None, help="运行时名（如 kimi/claude/glm/gpt）")
    ap.add_argument("--runtime-file", default=None)
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()
    if a.self_test:
        print(json.dumps(self_test(a.runtime, a.timeout), ensure_ascii=False, indent=2))
        return 0
    if not a.task:
        ap.error("需要 --task 或 --self-test")
    res = handle({"task": a.task, "runtime": a.runtime,
                  "runtime_file": a.runtime_file, "timeout": a.timeout})
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("rc") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
