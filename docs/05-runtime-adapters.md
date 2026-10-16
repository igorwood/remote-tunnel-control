# 运行时适配与"学会操作每台代理"

> 问题：每台代理上的 agent **各不相同** —— 有的是 Kimi，有的是 GLM，有的是 Claude，有的是 OpenAI，
> 有的甚至是自研 CLI。云端**不能**把调用方式写死成某一种。
> 答案：**统一契约 + 运行时适配层 + 能力探测学习 + 每机画像**。

---

## 1. 三层结构

```
云端                                            代理机器
┌──────────────────────────────┐                ┌───────────────────────────────┐
│ rmt.py / 任务派发             │  反向隧道      │ local_control_server（动作面）  │
│   ↓ 读画像（runtime/动作/超时）│ ─────────────▶ │   /agent（统一动作，推荐）      │
│ agent_probe.py（探测→学习）    │                │   └─▶ agent_bridge.py（任务桥） │
│ agentsdb.runtime_profiles     │                │        └─▶ agent_adapters.py    │
│   （画像：动作/能力/延迟/身份）│                │             ├─ CLI（kimi/glm/…) │
└──────────────────────────────┘                │             └─ HTTP（OpenAI 兼容）│
                                                └───────────────────────────────┘
```

| 文件 | 跑在哪 | 作用 |
|---|---|---|
| `agent_adapters.py` | 代理机 | **适配器层**：把任意运行时（CLI/HTTP）归一到 `run_task(cfg, task, timeout)` |
| `agent_bridge.py` | 代理机 | **任务桥**：控制面调它；也可 `--self-test` 输出**能力卡片**交给云端 |
| `agent_probe.py` | 云端 | **探测/学习**：发现调用动作、测工具能力与延迟、写画像 |
| `agentsdb.runtime_profiles` | 云端 | **每机画像**：`action`（用哪个动作）、`features`（能力）、`timings`、`samples`、来源 |

---

## 2. 统一契约（控制面应实现的动作）

```
POST /agent        （推荐名；兼容别名 /task、/run、旧名 /reasonix）
{"task": "自然语言任务", "timeout": 180, "runtime": "kimi" 可选}
→ {"ok": true,
   "output": {"rc": 0, "output": "…结果文本…", "runtime": "Kimi", "adapter": "cli_arg", "secs": 1.2}}
```
- `rc=0` 成功；`rc=127` 命令不存在；`rc=124` 超时；`rc=501` 未启用任务通道
- 未知端点应返回 `404`；动作未启用返回 `501`（云端据此**逐个试出**正确的动作名）

## 3. 适配器类型（`agent_adapters.py`）

| type | 说明 | 配置示例 |
|---|---|---|
| `cli_arg` | 任务作为命令行参数 | `{"type":"cli_arg","cmd":["claude","-p"],"label":"Claude Code"}` |
| `cli_stdin` | 任务写入 stdin | `{"type":"cli_stdin","cmd":["glm","chat"]}` |
| `cli_template` | 自定义模板（`{task}` 占位） | `{"cmd_tpl":["kimi","--prompt","{task}","--json"]}` |
| `openai_http` | OpenAI 兼容 HTTP | `{"base_url":"https://api.openai.com/v1","model":"gpt-4o-mini","api_key_env":"OPENAI_API_KEY"}` |
| `http_json` | 任意 HTTP + 响应路径 | `{"url":"…","body_tpl":{"q":"{task}"},"resp_path":"data.text"}` |
| `none` | 未启用任务通道（只读控制面） | — |

内建预设（**命令名/参数只是示例，按当地情况改**）：`kimi`、`glm`、`claude`、`openai`、`gemini`、`qwen`、`openai_compat`、`reasonix`。

**代理机配置**：项目根或 `scripts/` 放 `runtime.json`
```json
{ "default": "kimi",
  "kimi":   {"type": "cli_arg", "cmd": ["kimi", "--prompt"], "label": "Kimi"},
  "claude": {"type": "cli_arg", "cmd": ["claude", "-p"], "label": "Claude Code"},
  "gpt":    {"type": "openai_http", "base_url": "https://api.openai.com/v1",
             "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY", "label": "OpenAI"} }
```

## 4. 云端"学会"的流程

1. **接入时自述**（代理机）：`python scripts\agent_bridge.py --self-test`
   → 输出能力卡片（运行时/adapter/调用方式/平台/可用工具/自测结果），交给云端：
   `python3 agent_probe.py --self-report <machine> <卡片.json>`
2. **实地探测**（云端）：`python3 agent_probe.py <machine>` ——
   - 动作发现：逐个试 `/agent → /task → /run → /reasonix`，记下哪个可用（404/501 即跳过）
   - 能力探测：P1 基本调用 → P2 工具能力（能否读宿主环境）→ P4 长任务行为 → P5 身份自述
   - 延迟基准：连续 3 次，取中位 → 得出**建议超时** `max(60, 中位×20)`
   - 结论写入画像
3. **按画像派活**（云端）：`rmt.py run <machine> "<任务>"` 读画像 → 用学到的动作名与超时预算；
   画像显示未启用任务通道时**直接劝退**（不浪费调用）
4. **画像落库**：`agentsdb.runtime_profiles`，可用 `agent_probe.py --show`、`rmt.py profile <machine>` 查看

## 5. 实测样例（2026-10-16）

| 机器 | 学到的动作 | 任务通道 | 工具能力 | 建议超时 | 自述 |
|---|---|---|---|---|---|
| `machine-a`（Windows） | `/reasonix`（`/agent`、`/task`、`/run` 均 404） | ✅ | ✅（能读到主机名/cwd/时间） | 60s | "我是 Reasonix 编码代理（Windows + PowerShell 环境）" |
| `machine2`（Kimi 沙箱） | 无（四个候选动作全 501） | ❌ | — | — | "当前最小控制面未启用 reasonix 任意任务通道" |

派活验证：`rmt.py run machine-a "…"` 输出 `(动作 /reasonix，按画像)` 并成功执行；
`rmt.py run machine2 "…"` 被画像拦下并给出原因 —— **不再浪费调用、不再猜**。

## 6. 新增一种运行时要做什么

1. 在代理机的 `runtime.json` 里加一段（或直接用内建预设）
2. 跑 `agent_bridge.py --task "只回复 OK"` 自测
3. 把 `--self-test` 的能力卡片交给云端 → 云端 `agent_probe.py <machine>` 复核并落画像
4. 之后云端派活自动使用该运行时 —— **云端代码一行都不用改**
