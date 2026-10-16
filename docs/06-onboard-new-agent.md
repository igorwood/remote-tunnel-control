# 给 Kimi agent（machine2）的接入指令块

> 用途：直接整段转发给 machine2 上的 Kimi agent。云端随后会 `agent_probe.py machine2` 复核并写画像。
> 依据：2026-10-16 云端实测（隧道通、/ping /status 正常、但四个任务动作全 501、通道 B 无后端）。

---

【任务】让云端的统一控制面能调用你 —— 你是**第 2 台代理**（machine2 / Kimi 沙箱）

■ 现状（云端已实测，2026-10-16）
- 你的隧道已通：云端 `9100`（控制面）/ `9120`（agent 通道），主机 `MACHINE-B`
- 但四个候选**任务动作全部 501**：`/agent`、`/task`、`/run`、`/reasonix`
  → 返回「当前最小控制面未启用 reasonix 任意任务通道」，**云端无法给你派活**
- 通道 B（9120）后端无响应（连接被关闭）
- 你的 `/status` 里 `tunnel` 字段还是旧模板（`reverse (9000->9001, 9020->9020)`）

■ 目标：让 `POST /agent` 可用 + 通道 B 有后端 + 修掉描述

1) **下载套件**（不需要 SSH，随机路径）
   ```
   curl -L -o agent-kit.zip https://CLOUD_HOST/<kit-path>/agent-kit.zip
   ```
   解压到项目 `scripts/` 下：`agent_adapters.py`、`agent_bridge.py`、`agent_action.py`
   （另附 `runtime.example.json`、`KIT-README.md` 供参考）

2) **摸清你自己的运行时，并生成能力卡片**
   ```
   python3 agent_adapters.py --detect        # 看它发现哪些 CLI / API key
   # → 按结果写 runtime.json，把命令名/参数换成你真实的 Kimi 调用方式
   #   type 可选：cli_arg / cli_stdin / cli_template / openai_http / http_json
   python3 agent_bridge.py --task "只回复 OK" --runtime-file runtime.json   # 必须 rc=0
   python3 agent_bridge.py --self-test                                     # 输出能力卡片 JSON
   ```

3) **暴露统一动作**（二选一）
   - **A. 独立小服务（最快）**
     ```
     python3 agent_action.py --serve --port 9090 --token-file scripts/.agent_token
     ```
     然后把隧道映射改为：云端 `9120` ← 本机 `9090`（或另起一个端口映射）
   - **B. 接进你现有控制面（推荐）**
     ```python
     import agent_action
     if action in ("agent", "task", "run", "reasonix"):
         return agent_action.handle_action(payload)
     ```
   自测：
   ```
   curl -s -X POST http://127.0.0.1:9090/agent -H 'Content-Type: application/json' -d '{"task":"只回复 OK"}'
   ```
   期望：`{"ok":true,"output":{"rc":0,"output":"OK","runtime":"…","adapter":"…","secs":…}}`

4) **让通道 B（9120）有后端**：启动你的 agent serve，或把 9120 映射到 `agent_action` 的端口

5) **修 `/status` 里的 `tunnel` 描述**为你本机真实映射（现在是从别处抄来的旧值）

6) **回执 6 项**（不要贴令牌明文）
   ① 主机名　② 本地端口映射　③ `runtime.json` 的 `default` 与 `type`
   ④ `--self-test` 能力卡片 JSON　⑤ `/agent` 自测返回原文　⑥ 通道 B 是否已有后端

■ 安全红线
- 只绑 `127.0.0.1`；令牌只落本机文件；**不要**监听 `0.0.0.0`；密钥不写入任何会提交的文件

■ 云端随后会做
- 收到回执后云端跑 `agent_probe.py machine2` 实地复核（动作发现 + 工具能力 + 延迟基准）→ 写入该机画像
- 之后 `python3 rmt.py run machine2 "<任务>"` 即可直接给你派活；云端代码无需改动
