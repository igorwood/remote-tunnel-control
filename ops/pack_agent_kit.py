#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包「代理侧运行时套件」并挂一个 nginx 下载口（便于交接给候选代理，不需 SSH）。"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import zipfile

BASE = "/opt/remote-tunnel-control"
DIST = os.path.join(BASE, "dist")
CONF = "/etc/nginx/sites-available/geoplatform"
BACKUP = os.path.join(BASE, "backup")
KIT = ["agent_adapters.py", "agent_bridge.py", "agent_action.py",
       "runtime.example.json", "KIT-README.md"]

os.makedirs(DIST, exist_ok=True)

# ---- 1) 运行时配置模板与套件说明 ----
with open(os.path.join(BASE, "runtime.example.json"), "w", encoding="utf-8") as fh:
    json.dump({
        "default": "kimi",
        "_comment": "把下面的命令/参数换成你这台机器上真实的 agent 调用方式；type 可选 "
                    "cli_arg / cli_stdin / cli_template / openai_http / http_json",
        "kimi": {"type": "cli_arg", "cmd": ["kimi", "--prompt"], "label": "Kimi"},
        "claude": {"type": "cli_arg", "cmd": ["claude", "-p"], "label": "Claude Code"},
        "glm": {"type": "cli_stdin", "cmd": ["glm", "chat"], "label": "智谱 GLM"},
        "openai_compat": {"type": "openai_http", "base_url": "https://api.openai.com/v1",
                          "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY",
                          "label": "OpenAI 兼容 HTTP"},
        "http_generic": {"type": "http_json", "url": "http://127.0.0.1:8000/chat",
                         "body_tpl": {"prompt": "{task}"}, "resp_path": "reply",
                         "label": "自研 HTTP 接口"}
    }, fh, ensure_ascii=False, indent=2)

with open(os.path.join(BASE, "KIT-README.md"), "w", encoding="utf-8") as fh:
    fh.write("""# 代理侧运行时套件（agent-kit）

给你三样东西：**适配器**（把任何运行时归一）、**任务桥**（执行任务）、**统一动作服务**（让云端调得进来）。

## 文件
| 文件 | 作用 |
|---|---|
`agent_adapters.py` | 适配器层：cli_arg / cli_stdin / cli_template / openai_http / http_json |
`agent_bridge.py` | 任务桥：`--task "…"` 执行任务；`--self-test` 打印**能力卡片** |
`agent_action.py` | 统一动作服务：暴露 `POST /agent`（别名 /task /run /reasonix） |
`runtime.example.json` | 运行时配置模板（**必须按本机实际命令改**） |

## 三步
1. **摸清自己有什么**：`python3 agent_adapters.py --detect`
   → 输出建议的 runtime.json；存成 `runtime.json` 并按实际改命令名/参数
2. **自测**：`python3 agent_bridge.py --task "只回复 OK" --runtime-file runtime.json`
3. **对外提供统一动作**（二选一）：
   - 独立服务：`python3 agent_action.py --serve --port 9090 --token-file .agent_token`
     （然后把反向隧道映射到 9090）
   - 或在你现有控制面里：`import agent_action` → 动作名命中时 `return agent_action.handle_action(payload)`

## 自测清单
```
python3 agent_action.py --task "只回复 OK"          # 不经网络直跑
# 起服务后：
curl -s -X POST http://127.0.0.1:9090/agent -H 'Content-Type: application/json' \\
     -d '{"task":"只回复 OK"}'
curl -s http://127.0.0.1:9090/ping
```
返回形如 `{"ok":true,"output":{"rc":0,"output":"OK","runtime":"…","adapter":"…","secs":0.8}}` 即合格。
""")

# ---- 2) 打 kit 包 ----
zip_path = os.path.join(DIST, "agent-kit.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for f in KIT:
        p = os.path.join(BASE, f)
        if os.path.exists(p):
            z.write(p, f)
info = zipfile.ZipFile(zip_path)
bad = info.testzip()
print("① 套件已打包: %s（%d 个文件，%.1f KB，完整性 %s）"
      % (zip_path, len(info.namelist()), os.path.getsize(zip_path) / 1024,
         "OK" if bad is None else "损坏:" + str(bad)))
print("   内含: %s" % ", ".join(info.namelist()))

# ---- 3) 挂 nginx 下载口（随机路径段，非敏感文件）----
tok_path = None
try:
    seg_file = os.path.join(BASE, "dist", ".kit_path")
    seg = open(seg_file, encoding="utf-8").read().strip() if os.path.exists(seg_file) else \
        "kit-" + secrets.token_hex(6)
    with open(seg_file, "w", encoding="utf-8") as fh:
        fh.write(seg)
    tok_path = "/%s/" % seg

    src = open(CONF, encoding="utf-8").read()
    if ("location ^~ %s" % tok_path) in src:
        print("② nginx 下载口已存在: https://CLOUD_HOST%sagent-kit.zip" % tok_path)
    else:
        os.makedirs(BACKUP, exist_ok=True)
        bak = os.path.join(BACKUP, "geoplatform.nginx.%s.bak" % time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(CONF, bak)
        block = """
    # ==== 代理运行时套件下载（非敏感文件，随机路径段；由 ops/pack_agent_kit.py 维护）====
    location ^~ %s {
        alias %s/;
        autoindex off;
        default_type application/octet-stream;
    }
""" % (tok_path, DIST)
        anchor = "\n    location / {"
        if src.count(anchor) != 1:
            print("② [ABORT] nginx 锚点不唯一，未改配置")
        else:
            open(CONF, "w", encoding="utf-8", newline="").write(src.replace(anchor, block + anchor, 1))
            rc = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
            if rc.returncode != 0:
                shutil.copy2(bak, CONF)
                print("② [ROLLBACK] nginx -t 失败，已回滚:\n%s" % (rc.stdout + rc.stderr)[:300])
            else:
                subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True)
                print("② nginx 下载口已挂: https://CLOUD_HOST%sagent-kit.zip（已 reload）" % tok_path)
except Exception as e:
    print("② nginx 挂载异常: %s: %s" % (type(e).__name__, e))

# ---- 4) 公网实测下载 ----
if tok_path:
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = "https://CLOUD_HOST%sagent-kit.zip" % tok_path
    try:
        with urllib.request.urlopen(url, timeout=25, context=ctx) as r:
            data = r.read()
        print("③ 公网下载实测: HTTP %s，%d 字节（本地 %d 字节）%s"
              % (r.status, len(data), os.path.getsize(zip_path),
                 "✓ 一致" if len(data) == os.path.getsize(zip_path) else "✗ 不一致"))
        print("   下载地址（交给代理）: %s" % url)
    except Exception as e:
        print("③ 公网下载实测失败: %s: %s" % (type(e).__name__, e))
