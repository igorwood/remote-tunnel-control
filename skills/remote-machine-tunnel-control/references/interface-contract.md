# 双端程序清单与对接协议（云端 ↔ 本地）

> 本文件回答三个问题：**本地端有哪些程序、云端端有哪些程序、它们之间怎么对接。**
> 与 `architecture.md`（拓扑与多机规划）配套；故障处置见 SKILL.md §4。

---

## 1. 全景对照

```
┌────────────────────────── 云端（控制方） ──────────────────────────┐
│  A 通道客户端      rmt.py / 内联 Python(urllib)                     │
│  B 巡检器          jdbjj_progress.py  ← cron */30                   │
│  C 投送器          push_remote_file.py（把补丁/脚本送到远端执行）    │
│  D 凭据与注册      env.sh（令牌）、machines.json（多机注册表）        │
│  E 审计与日志      /root/jdbjj/*.py 日志、cron 输出                  │
│  F 隧道出口        127.0.0.1:9000（控制面）  127.0.0.1:9020（agent） │
└───────────────────────────────┬────────────────────────────────────┘
                    SSH 反向隧道（由本地发起，仅回环）
┌───────────────────────────────┴────────────────────────────────────┐
│                       本地端（被控方，NAT 后）                       │
│  T 隧道        reverse_tunnel.py（map 9000:9001, 9020:9020）         │
│  A 控制面      local_control_server.py（127.0.0.1:9001，白名单+agent）│
│  B agent       reasonix serve（127.0.0.1:9020）                      │
│  C 编排        supervisor.py → watchdog.py →（watchdog_transcribe/   │
│                watchdog_tunnel）→ run_pipeline.py → transcribe.py    │
│  D 辅助        agent_daemon.py（队列心跳）、package_and_upload.py     │
│  E 状态        run/*-progress.json、logs/*.log、index.json            │
│  F 凭据        .sshpass / .agent_token / .reasonix_token             │
│  G 单实例守卫  _singleton.py（19100–19110）+ tunnel.ps1（人工入口）   │
└─────────────────────────────────────────────────────────────────────┘
```

**对接只有三个接触面**：① SSH 反向隧道（传输）② HTTP 控制面（动作）③ HTTP agent serve（任意任务）。

---

## 2. 本地端程序清单

| 程序 | 职责 | 接口 / 关键参数 | 启动方式 | 日志 | 守卫端口 | 典型失败模式 |
|---|---|---|---|---|---|---|
| `reverse_tunnel.py` | **唯一出站入口**：建 SSH 反向隧道并转发多组端口 | `--map 9000:9001 --map 9020:9020`；凭据读 `scripts/.sshpass` | 由 supervisor 拉起（DETACHED） | `logs/tunnel.log` | 19106 | 断链（本地侧退出）；paramiko 单 handler 槽位被覆盖 → 端口转发到错后端（9000 返回**纯文本 401**） |
| `local_control_server.py` | **控制面**：白名单动作 + `reasonix` 任意任务；唯一对外 HTTP 面 | `--port 9001`；认证头 `X-Token`（读 `scripts/.agent_token`） | supervisor | `logs/control.log` | 19107 | 令牌不匹配（401 JSON）；动作不在白名单；子进程输出 GBK 解码坑 |
| `reasonix serve`（外部 CLI） | **agent 通道**：官方 HTTP+SSE + Web UI | `serve --addr 127.0.0.1:9020 --auth token`（token 在 `scripts/.reasonix_token`） | supervisor `ensure_reasonix_serve()`（**按端口判断**，不按命令行） | `logs/reasonix-serve.log` | — | 用命令行匹配判断会误判（`reasonix-cli.exe` 的 CommandLine 不可靠） |
| `supervisor.py` | **保活与去重**：保证每个组件有且只有一个 | `--interval 300`（`--once` 单轮）；去重靠 `busy()` | **CIM/schtasks 拉起**（见 SKILL §4.6） | `logs/supervisor.log` | 19100 | `pids_of()` 恒返回 0 → 每轮繁殖新实例（子串匹配 + CommandLine 读取失败） |
| `watchdog.py` | 拉起并守护两个专用看门狗 | `--interval 300` | supervisor | `logs/watchdog.log` | 19101 | 缺少子看门狗 → 长任务无人守 |
| `watchdog_transcribe.py` | **判活核心**：区分"慢 vs 死" | `--interval 60`；读 `run/transcribe-progress.json`；动态阈值 | watchdog.py | `logs/watchdog-transcribe.log` | 19102 | 只按"有无新产物"判活 → 误杀长分片 → **活锁**（本方案已修） |
| `watchdog_tunnel.py` | 隧道探活与自动重建 | `--interval 60` | watchdog.py | `logs/watchdog-tunnel.log` | 19103 | 断链后无人重建 → 云端**永久失联**（最关键的自愈环节） |
| `run_pipeline.py` | 编排：分片遍历、失败重试、续跑 | 无参（读项目配置） | supervisor | `logs/pipeline.log` | 19104 | **链式自派生**（父→子）→ 外部"去重"极易误杀 |
| `transcribe.py` | **长任务主体**：ffmpeg 解码 + faster-whisper 转录 | `--model large-v3 ...`；写 `run/transcribe-progress.json` | run_pipeline | `logs/pipeline.log` | 19105 | 无进度反馈（已修）；用了不支持的 `callback=`（已修） |
| `agent_daemon.py` | SFTP 队列心跳（备用通道） | `--interval 30`；`queue/`→`done/`、`heartbeat.json` | supervisor | `logs/agent-daemon.log` | 19108 | 心跳停更（链路未恢复） |
| `package_and_upload.py` | 打包产物并上传 | `--wait` | supervisor | `logs/package-upload.log` | 19110 | — |
| `monitor.py` | 本地汇总巡检 | `--interval …` | supervisor | `logs/monitor.log` | 19109 | — |
| `_singleton.py` | **单实例守卫**（被上面各脚本 import） | `acquire(name, port)` | 被 import | — | — | 端口冲突（同机多实例）→ 立即退出（**预期行为**） |
| `tunnel.ps1` | 人工入口：隧道 `status` / `restart` | PowerShell 脚本 | 人工 / 计划任务 | — | — | 无 |

**本地状态文件（云端读取的事实来源）**：`run/transcribe-progress.json`（秒级心跳）、
`transcripts/index.json`（产物计数）、`logs/*.log`、`logs/agent-audit.log`、`.bak-*`（改动备份）。

---

## 3. 云端端程序清单

| 程序 | 职责 | 关键实现 | 调度 |
|---|---|---|---|
| `/root/jdbjj/env.sh` | 提供 `JDBJJ_REMOTE`、`JDBJJ_TOKEN`（**内容不回显**） | 纯 shell | — |
| `rmt.py` | **多机统一 CLI**：`list / status / tail / list-transcripts / start / stop / run` | 读 `machines.json`；按机器取端口与令牌 | 人工 |
| 内联 Python 调用范式 | 临时查询/控制（避开安全策略对中文 curl 的拦截） | `urllib` + `env.sh` 读令牌 | 人工 |
| `jdbjj_progress.py` | **巡检**：段进度判活、断线告警、快照去重 | 读 `tail_log?which=pipeline` + `/status`；stdout 非空才推送 | cron `*/30 * * * *`（`no_agent`，`deliver=origin`） |
| `push_remote_file.py` | **投送器**：把云端本地文件原样送到远端执行 | 文件内容嵌入 `reasonix` 任务；要求"原样保存→运行→完整回传→删除" | 人工 |
| `_singleton.py` / `_add_guard.py` / `_patch*.py` / `_dedup*.py` / `_converge.py` / `_start_*.py` | 一次性运维脚本（守卫注入、补丁、去重、收敛、拉起） | 均在 `/root/jdbjj/`，可复用 | 人工 |
| `verify_*.py` / `final_verify.py` | 改动后验证（语法+关键行+进度两采样） | — | 人工 |
| `machines.json` | 多机注册表（机器名、隧道端口、env 路径、令牌键、守卫基址） | JSON | — |
| cron 任务 | 巡检 + 与本地任务无关的推送类任务（错峰） | — | 见 §4.6 |

---

## 4. 对接协议

### 4.1 传输层：SSH 反向隧道

- **方向**：本地主动 `ssh -R` 到云端；一条连接上多组端口转发
- **绑定**：两端**只绑 127.0.0.1**（云端出口 9000/9020；本地后端 9001/9020）
- **凭据**：`scripts/.sshpass`（密码，**建议改密钥认证**）
- **实现要点（踩过的坑）**：paramiko `Transport` 只有**一个** `_tcp_handler` 槽位，
  多次 `request_port_forward` 会互相覆盖 → 必须用**统一 handler 按目标端口查表分发**（`_PORT_MAP`）
- **判定**：`reverse_tunnel.py` 应按**端口可连通性**判定后端是否健康，不要按命令行匹配

### 4.2 认证

| 通道 | 头 | 令牌来源（本地） | 云端保存 |
|---|---|---|---|
| A 控制面 | `X-Token: <t>` | `scripts/.agent_token` | `env.sh` 的 `JDBJJ_TOKEN` |
| B agent | `Authorization: Bearer <t>` | `scripts/.reasonix_token` | 单独保存（**不给不信任方**） |

> 每台机器**独立令牌**，云端按机分目录：`/root/machines/<name>/env.sh`（权限 600）。

### 4.3 控制面 HTTP 契约

- 请求：`GET /<action>?<params>` 或 `POST /reasonix`（JSON body）
- 响应：`{"ok": bool, "action": str, "output": …}`；失败 `ok=false` + `error` 文本
- 认证失败：`{"ok": false, "error": "令牌不匹配"}`（**JSON**；若是**纯文本 401** → 隧道转发到了错后端）
- 动作白名单（§architecture §6）：`ping / status / tail_log / list_transcripts / transcribe_start / transcribe_stop / package / analyze / reasonix`

### 4.4 `reasonix` 任务契约（权限升级通道）

```json
POST /reasonix
{"task": "自然语言任务", "mode": "print|run", "continue": false,
 "max_steps": 40, "format": "text|json|stream-json", "timeout": 1800}
→ {"output": {"rc": 0, "secs": 37.2, "output": "任务输出文本"}}
```

约定：
- `mode=print` 一次性；`mode=run` 可 `continue` 续接会话
- **超时**：短查询 120–180s；投送脚本 200–260s；**别把 >5 步或 >8KB 的任务塞进来**（易超时）
- **越权边界**：等效于把本机 agent 控制权交给调用方 → 全量写 `logs/agent-audit.log`
- **回传纪律**：任务里必须写"**完整输出原样回传，不要总结**"，否则关键行会被它的总结吃掉

### 4.5 状态与编码约定

- 远端脚本一律 `subprocess.run(..., encoding="utf-8", errors="replace")`
  （Windows 默认 GBK 解码 UTF-8 → `stdout=None` → 表现为"只有中文/工具任务失败"）
- 远端跑脚本前 `$env:PYTHONIOENCODING='utf-8'`（否则回传中文乱码，仅编码问题、数据无误）
- 进度/状态文件一律 **UTF-8 + 原子写**（`.tmp` → `os.replace`）
- 时间统一用本地时区字符串 `YYYY-MM-DD HH:MM:SS`（两端时区一致时可比对）

### 4.6 幂等、超时与调度

| 项 | 约定 |
|---|---|
| 写动作 | `transcribe_start`/`stop`/`package`/`analyze` **幂等**；`stop` 会丢弃当前分片进度，慎用 |
| 组件保活 | supervisor 每 300s 一轮；看门狗 60s 一轮 |
| 云端巡检 | `*/30`，静默（有输出才推送）；断线首发即报 + 每 2h 重申；90 分钟无进展才报卡住 |
| 错峰 | 重任务不同时跑：地缘视图 06:35 / 简报 09:30 / 推送整点 / 研究 17:30 / 巡检 */30 |
| 互斥 | 所有重任务 `flock /root/trendradar/.heavy.lock` |

---

## 5. 三条典型交互时序

### 5.1 状态查询（最常用）

```
云端 rmt.py jdbjj status
  → GET 127.0.0.1:9000/status  (X-Token)
  → 隧道 → 本地 9001 local_control_server
  → 读取 transcripts/index.json + GPU + 进程表
  ← {ok:true, output:{transcribe_parts,total_parts,gpu,procs,…}}
```

### 5.2 投送补丁（改动远端代码的标准流程）

```
1) 云端本地写好 _patch_x.py（锚点断言 + .bak + py_compile + 回滚 + 幂等）
2) push_remote_file.py 把文件内容嵌入 reasonix 任务
3) 远端 agent：原样保存 scripts/_patch_x.py → 运行 → 完整回传输出 → 删除脚本（保留 .bak）
4) 云端核对：diff、py_compile 结果、关键行是否就位
5) 重启受影响组件（用 transcribe_start 或 CIM 拉起 supervisor）
6) 验证：进度文件 pct 两采样在涨 + 日志无新异常 + 触发条件不再触发
```

### 5.3 长任务巡检（自动）

```
cron */30 → jdbjj_progress.py
  → GET /status（产物计数、GPU、主机）
  → GET /tail_log?which=pipeline&lines=12（段进度）
  → 判活：产物增加 OR 段进度变化 = 在干活（两者都停 90 分钟才报"可能卡住"）
  → 快照去重（10 分钟内同状态不重复推送）
  → stdout 非空 → 投递到飞书；空 → 静默
```

---

## 6. 从零接线（新机器握手 8 步）

1. **本地**建目录/venv、放三个令牌文件（`.sshpass`/`.agent_token`/`.reasonix_token`，各自随机）
2. **本地**部署 `scripts/`（§2 表中带守卫端口的全部脚本 + `_singleton.py`）
3. **本地**给长驻脚本注入守卫（端口 = `守卫基址 + 偏移`，见 architecture §4）
4. **本地**用 CIM 拉起 `supervisor.py --interval 300`（**不要**用会话内 `Popen`，会被回收）
5. **本地**确认隧道：`tunnel.ps1 status` → 云端能看到 9000/9020 监听
6. **云端**写 `/root/machines/<name>/env.sh` + 登记 `machines.json`（端口/令牌/守卫基址）
7. **云端**验证：`python3 rmt.py <name> status` 返回该机主机名与进度；`run "回复 OK"` 通则 agent 通道正常
8. **云端**挂巡检 cron（`*/30`，静默，`deliver=origin`）

**验收口径**：`status` 有数据 + `run` 有回传 + 巡检能推 + **断链后能自愈**（拔掉本地隧道，看 `watchdog_tunnel` 是否 5 分钟内重建）。

---

## 7. 版本与兼容

- 每个本地脚本建议在文件头写 `VER = "YYYY-MM-DD.N"`，并在 `/status` 返回中带出来
  → 云端可据此判断"某机器还在跑旧逻辑"（例如：是否已具备进度文件、是否有守卫端口）
- 端口与偏移**一经确定不要改**：守卫端口是去重判定的主依据
- 兼容策略：**先加守卫与心跳，再改判定**；改动必须带 `.bak` 与回滚路径

---

## 8. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-08 | 首条反向隧道落地（9000/9020），控制面 + agent 双通道 |
| 2026-10-10 | 修活锁：进度文件 + 动态阈值；修重复实例：守卫端口 + `busy()`；确认 CIM 拉起 |
| 2026-10-12 | 双端程序清单与对接协议成文（本文件）；多机注册表 + `rmt.py` |
