#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_adapters.py —— 运行时适配层（**不绑定任何具体 agent**）

统一契约：无论远端跑的是 Kimi / GLM / Claude / GPT / Gemini / 自研 CLI，
都归一到同一个调用结果：

    run_task(runtime_cfg, task, timeout) -> {"rc": int, "output": str,
                                             "runtime": str, "adapter": str, "secs": float}

适配器类型（type）：
  cli_arg      → 把任务作为命令行参数：  cmd: ["claude", "-p"]         → 执行 claude -p "<task>"
  cli_stdin    → 把任务写进 stdin：      cmd: ["kimi", "--stdin"]
  cli_template → 自定义模板（含 {task}）：cmd_tpl: ["glm", "chat", "--message", "{task}"]
  openai_http  → OpenAI 兼容 HTTP：      base_url / model / api_key_env
  http_json    → 任意 HTTP 接口：        url / body_tpl / resp_path（从响应里取文本）
  none         → 该机器未启用任务通道（只读控制面）

机器侧配置示例（runtime.json / env）见 RUNTIME_PRESETS。
**注意：命令名与参数只是示例** —— 各机器的实际 CLI 名/参数请按当地情况改。
"""
import json
import os
import subprocess
import time
import urllib.request

# ---------------- 预设（示例，可按机器覆盖） ----------------
RUNTIME_PRESETS = {
    "kimi":     {"type": "cli_arg", "cmd": ["kimi", "--prompt"], "label": "Kimi"},
    "glm":      {"type": "cli_arg", "cmd": ["glm", "--prompt"], "label": "智谱 GLM"},
    "claude":   {"type": "cli_arg", "cmd": ["claude", "-p"], "label": "Claude Code"},
    "openai":   {"type": "cli_arg", "cmd": ["openai", "api", "chat.completions.create"],
                 "label": "OpenAI CLI"},
    "gemini":   {"type": "cli_arg", "cmd": ["gemini", "-p"], "label": "Gemini CLI"},
    "qwen":     {"type": "cli_arg", "cmd": ["qwen", "-p"], "label": "Qwen CLI"},
    # 通用兜底：任何"吃 stdin 吐 stdout"的 CLI
    "cli_stdin_generic": {"type": "cli_stdin", "cmd": ["python", "-c",
                          "import sys;print('请在此接入你的 agent CLI:', sys.stdin.read())"],
                          "label": "通用 stdin 适配器（需改成你的命令）"},
    # HTTP/OpenAI 兼容
    "openai_compat": {"type": "openai_http", "base_url": "https://api.openai.com/v1",
                      "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY",
                      "label": "OpenAI 兼容 HTTP"},
    # 兼容旧实现：本机控制面直接暴露 reasonix 动作
    "reasonix": {"type": "local_control", "action": "reasonix", "label": "reasonix（旧通道）"},
    "none":     {"type": "none", "label": "未启用任务通道"},
}


# ---------------- 各适配器实现 ----------------
def _cli_arg(cfg, task, timeout):
    cmd = list(cfg["cmd"]) + [task]
    return _run(cmd, None, timeout)


def _cli_stdin(cfg, task, timeout):
    return _run(list(cfg["cmd"]), task, timeout)


def _cli_template(cfg, task, timeout):
    cmd = [x.replace("{task}", task) for x in cfg["cmd_tpl"]]
    return _run(cmd, None, timeout)


def _run(cmd, stdin_text, timeout):
    t0 = time.time()
    try:
        p = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return {"rc": p.returncode, "output": (p.stdout or "") + (p.stderr or ""),
                "secs": round(time.time() - t0, 1)}
    except FileNotFoundError:
        return {"rc": 127, "output": "命令不存在: %s（请在 runtime 配置里改成实际的 CLI 名）" % cmd[0],
                "secs": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "output": "超时（%ss）" % timeout, "secs": round(time.time() - t0, 1)}


def _openai_http(cfg, task, timeout):
    key = os.environ.get(cfg.get("api_key_env", "OPENAI_API_KEY"), "")
    if not key:
        return {"rc": 3, "output": "缺少环境变量 %s" % cfg.get("api_key_env"), "secs": 0.0}
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps({"model": cfg.get("model"), "messages": [{"role": "user", "content": task}]}
                      ).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return {"rc": 0, "output": txt, "secs": round(time.time() - t0, 1)}
    except Exception as e:                                   # noqa: BLE001
        return {"rc": 1, "output": "%s: %s" % (type(e).__name__, e),
                "secs": round(time.time() - t0, 1)}


def _http_json(cfg, task, timeout):
    payload = _fill(cfg.get("body_tpl", {"task": "{task}"}), task)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        cur = d
        for part in (cfg.get("resp_path") or "output").split("."):
            cur = cur[part] if isinstance(cur, dict) else cur
        return {"rc": 0, "output": cur if isinstance(cur, str) else json.dumps(cur, ensure_ascii=False),
                "secs": round(time.time() - t0, 1)}
    except Exception as e:                                   # noqa: BLE001
        return {"rc": 1, "output": "%s: %s" % (type(e).__name__, e),
                "secs": round(time.time() - t0, 1)}


def _fill(obj, task):
    if isinstance(obj, str):
        return obj.replace("{task}", task)
    if isinstance(obj, dict):
        return {k: _fill(v, task) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, task) for v in obj]
    return obj


DISPATCH = {"cli_arg": _cli_arg, "cli_stdin": _cli_stdin, "cli_template": _cli_template,
            "openai_http": _openai_http, "http_json": _http_json}


def load_runtime(spec=None, runtime_file=None, kind=None):
    """取运行时配置：spec 直接给 dict > runtime_file > 预设 kind（默认 none）。"""
    if spec:
        return dict(spec)
    if runtime_file and os.path.exists(runtime_file):
        with open(runtime_file, encoding="utf-8") as fh:
            d = json.load(fh)
        if kind and kind in d:                       # 文件里可放多套，按名字选
            return d[kind]
        return d
    return dict(RUNTIME_PRESETS.get(kind or "none", RUNTIME_PRESETS["none"]))


def run_task(runtime_cfg, task, timeout=180):
    """统一入口：按适配器类型执行任务，返回归一结果。"""
    cfg = dict(runtime_cfg or {})
    t = cfg.get("type", "none")
    label = cfg.get("label") or t
    if t == "none":
        return {"rc": 501, "output": "该机器未启用任务通道（runtime type=none）",
                "runtime": label, "adapter": "none", "secs": 0.0}
    if t == "local_control":                         # 交回控制面自己的动作（旧 reasonix 通道）
        return {"rc": 502, "output": "local_control 适配器应由本机控制面处理（action=%s）"
                % cfg.get("action"), "runtime": label, "adapter": t, "secs": 0.0}
    fn = DISPATCH.get(t)
    if not fn:
        return {"rc": 501, "output": "未知适配器类型: %s" % t, "runtime": label,
                "adapter": t, "secs": 0.0}
    r = fn(cfg, task, timeout)
    r.update({"runtime": label, "adapter": t})
    return r


def detect_environment():
    """扫描本机，**推断**这台机器适合哪种运行时，并给出建议的 runtime.json。

    这是"让代理自我描述"的第一步：不假设，只看现场有什么。
    """
    import shutil
    hits, envkeys = {}, {}
    for name, spec in RUNTIME_PRESETS.items():
        cmd = list(spec.get("cmd") or [])
        if cmd and shutil.which(cmd[0]):
            hits[name] = {"type": spec["type"], "cmd": cmd, "label": spec.get("label"),
                          "resolved": shutil.which(cmd[0])}
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MOONSHOT_API_KEY",
              "KIMI_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY", "GEMINI_API_KEY",
              "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "QWEN_API_KEY"):
        if os.environ.get(k):
            envkeys[k] = "已设置（%d 字符，值隐藏）" % len(os.environ[k])
    # 建议的默认运行时（按优先级：本地 CLI 优先，其次 HTTP API）
    order = ["claude", "kimi", "glm", "openai", "gemini", "qwen"]
    default = next((k for k in order if k in hits), None)
    suggested = {}
    for k, v in hits.items():
        suggested[k] = {"type": v["type"], "cmd": v["cmd"], "label": v["label"]}
    if not default and any(k in envkeys for k in ("OPENAI_API_KEY", "MOONSHOT_API_KEY",
                                                  "KIMI_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY",
                                                  "ANTHROPIC_API_KEY")):
        default = "openai_compat"
        suggested["openai_compat"] = dict(RUNTIME_PRESETS["openai_compat"])
    if not default:
        suggested["__note__"] = ("未发现可用的 agent CLI 或 API 密钥：请手工填写 runtime.json "
                                 "（type 可选 cli_arg/cli_stdin/cli_template/openai_http/http_json）")
    else:
        suggested["default"] = default
    return {"suggested_runtime_json": suggested, "detected_cli": hits,
            "detected_api_keys": envkeys,
            "platform": __import__("platform").platform(),
            "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S")}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="运行时适配器：自测 / 环境推断")
    ap.add_argument("--kind", default=None, help="预设名，如 kimi/glm/claude/openai")
    ap.add_argument("--file", default=None, help="runtime.json 路径")
    ap.add_argument("--task", default="只回复 OK")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--detect", action="store_true", help="扫描本机并建议 runtime.json")
    ap.add_argument("--write", default=None, metavar="PATH", help="把建议写入 runtime.json")
    a = ap.parse_args()
    if a.detect:
        d = detect_environment()
        out = json.dumps(d, ensure_ascii=False, indent=2)
        if a.write:
            with open(a.write, "w", encoding="utf-8") as fh:
                json.dump(d["suggested_runtime_json"], fh, ensure_ascii=False, indent=2)
            print("已写入 %s（请人工核对命令名/参数后再用）" % a.write)
        print(out)
        sys.exit(0)
    cfg = load_runtime(runtime_file=a.file, kind=a.kind)
    print("运行时配置:", json.dumps(cfg, ensure_ascii=False))
    print("结果:", json.dumps(run_task(cfg, a.task, a.timeout), ensure_ascii=False))
    print("\n可用预设:", ", ".join(sorted(RUNTIME_PRESETS)))
