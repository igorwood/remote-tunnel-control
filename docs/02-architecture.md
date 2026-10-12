# 云端 ↔ 多台本地机器 · 反向隧道控制架构

> 版本：2026-10-12 ｜ 首个落地实例：`MACHINE-A`（Windows，JDBJJ 转录流水线）
> 配套 skill：`remote-machine-tunnel-control`（故障处置与防崩溃细节以 skill 为准）

---

## 1. 目标与约束

**目标**：云端机器能够**远程查看与控制**位于 NAT 后、无公网 IP 的多台本地机器，完成长任务
（转录、抓取、批处理）的启停、进度观测、代码修补与故障恢复。

**约束**：

- 本地机器**不能开放任何入站端口**（家庭/办公网络，无端口映射）
- 云端**不开放对外端口**（只有一个 SSH 入口用于运维）
- 两端的转发端口**只绑回环**，外网扫描不到
- 稳定性优先级高于功能：**长任务跑得住**比"能控制"更重要

---

## 2. 拓扑

```
       ┌────────────── 云端服务器 ──────────────┐        ┌──────── 本地机器 A（NAT 后）────────┐
       │ 127.0.0.1:9000  ← 反向隧道出口         │        │ 127.0.0.1:9001  local_control_server │
       │ 127.0.0.1:9020  ← 反向隧道出口         │◀══╗    │ 127.0.0.1:9020  reasonix serve       │
       │                                        │   ║    │                                     │
       │ SSH 客户端(22) ◀───────────────────────────╝    │ reverse_tunnel.py（主动外连）        │
       └────────────────────────────────────────┘        └──────────────────────────────────────┘
                                                          ▲ 同一套脚本复制到 B / C / D …
```

- 反向隧道由**本地主动发起**（`ssh -R` 语义），所以本地不需要任何入站能力
- 一条 SSH 连接上**同时转发多组端口**（控制面 + agent serve）
- 支持 N 台机器：每台机器**独立令牌 + 独立端口段**

---

## 3. 两条通道与认证

| 通道 | 云端地址 | 本地后端 | 能力 | 认证 |
|---|---|---|---|---|
| **A 控制面** | `http://127.0.0.1:9000/…` | `local_control_server.py`（本地 9001） | 白名单动作 + `reasonix` 任意任务 | `X-Token: <agent_token>` |
| **B agent** | `http://127.0.0.1:9020/…` | `reasonix serve` | agent 官方 HTTP+SSE、Web UI | `Authorization: Bearer <reasonix_token>` |

**通道 A 的设计意图**：平时用**白名单动作**（只读状态/日志 + 幂等的启停），
需要复杂操作时才用 `reasonix` 动作把**自然语言任务**交给本地 agent 执行 —— 这是权限升级，
每次调用都会写本地 `logs/agent-audit.log`。

**通道 B 的用途**：复杂交互（多轮、看 UI）时，在自己电脑上 `ssh -N -L 9020:127.0.0.1:9020 root@«云主机»`
把 serve 拉到自己浏览器。

---

## 4. 端口分配规范（多机）

| 机器 | 隧道端口（控制面 / agent） | 守卫端口段（单实例锁） | 令牌文件（本地） |
|---|---|---|---|
| `MACHINE-A` | 9000 / 9020 | 19100 – 19110 | `scripts/.agent_token`、`.sshpass`、`.reasonix_token` |
| `MACHINE-B` | 9100 / 9120 | 19200 – 19210 | 同上（**独立令牌**） |
| `MACHINE-C` | 9200 / 9220 | 19300 – 19310 | 同上（**独立令牌**） |

**规则**：`隧道端口 = 9000 + 100×序号`，`守卫端口段 = 19100 + 100×序号`，永远不要复用令牌。

守卫端口明细（与 `_singleton.py` 一致，`guard_base=19100`）：

| 偏移 | 脚本 | 偏移 | 脚本 |
|---|---|---|---|
| +0 | `supervisor.py` | +6 | `reverse_tunnel.py` |
| +1 | `watchdog.py` | +7 | `local_control_server.py` |
| +2 | `watchdog_transcribe.py` | +8 | `agent_daemon.py` |
| +3 | `watchdog_tunnel.py` | +9 | `monitor.py` |
| +4 | `run_pipeline.py` | +10 | `package_and_upload.py` |
| +5 | `transcribe.py` | | |

---

## 5. 本地组件与启动链

```
supervisor.py ──┬── run_pipeline.py ──┬── transcribe.py        （长任务主体，写 run/transcribe-progress.json）
                │                     └── （分片续跑 / 失败重试）
                ├── local_control_server.py（--port 9001，白名单动作 + reasonix）
                ├── reverse_tunnel.py（--map 9000:9001 --map 9020:9020）
                ├── agent_daemon.py（--interval 30，SFTP 队列心跳）
                ├── package_and_upload.py（--wait，打包上传）
                └── watchdog.py ──┬── watchdog_transcribe.py（--interval 60，按进度文件判活）
                                  └── watchdog_tunnel.py（--interval 60，隧道探活重启）
```

**职责边界**：

- `supervisor.py`：保证"每个组件有且只有一个"（去重用 `busy()`，见 skill §4.5）
- `watchdog_transcribe.py`：区分"慢/死"（进度文件新鲜度 + 动态阈值，见 skill §4.1）
- `watchdog_tunnel.py`：隧道断了自动重建（**本地唯一能在断链后自愈的环节**，最重要）
- `local_control_server.py`：唯一对外暴露的 HTTP 面（白名单）

---

## 6. 控制面 API 参考

统一返回 `{"ok": true|false, "action": "...", "output": {...}|"错误"}`。

| 动作 | 方法 | 参数 | 返回要点 |
|---|---|---|---|
| `ping` | GET | — | 存活 |
| `status` | GET | — | `host/time/transcribe_parts/total_parts/audio_parts/gpu/model/disk_free_gb/procs` |
| `tail_log` | GET | `which=pipeline\|audit\|control\|…`、`lines=N` | `{file, content, lines}` |
| `list_transcripts` | GET | — | `{count, files:[{path,size}]}` |
| `transcribe_start` | GET | — | 幂等启动/恢复流水线，返回 `procs` |
| `transcribe_stop` | GET | — | 终止转录进程组，返回 `killed` 列表 |
| `package` | GET | — | 打包 `transcripts/` 为 zip |
| `analyze` | GET | — | 重跑结构化分析 |
| `reasonix` | POST | `{task, mode=print\|run, continue, max_steps, format, timeout}` | `{rc, secs, output}`；**权限升级，见 §3 警告** |

> 只读动作（`status`/`tail_log`/`list_transcripts`）可安全高频调用；写动作（`transcribe_*`/`package`/`analyze`）
> 幂等，但**别在长任务关键节点乱调**（`transcribe_stop` 会丢弃当前分片进度）。

---

## 7. 云端调用范式

见 skill §2：**一律用 Python + `urllib`**（避开安全策略对中文内联 curl 的拦截），
令牌从 `env.sh` 读且**不回显**。

健康检查一条龙：

```python
st = get("status")["output"]
print(st["transcribe_parts"], "/", st["total_parts"], "| GPU", st["gpu"], "| 主机", st["host"])
print(get("tail_log?which=pipeline&lines=8")["output"]["content"])
```

**投送脚本/补丁**（最常用的写操作）：把文件内容嵌进 `reasonix` 任务，要求
"原样保存 → 运行 → 完整回传 → 删除"，见 skill §2 与 `scripts/push_remote_file.py`。

---

## 8. 多机注册与统一 CLI

`/root/jdbjj/machines.json`（云端）：

```json
{
  "machines": {
    "jdbjj": {
      "desc": "Windows 台式机 · 转录流水线",
      "control_port": 9000,
      "serve_port": 9020,
      "env": "/root/jdbjj/env.sh",
      "token_key": "JDBJJ_TOKEN",
      "guard_base": 19100
    }
  }
}
```

统一 CLI（`scripts/rmt.py`）：

```bash
python3 rmt.py list                      # 列出所有机器 + 在线状态
python3 rmt.py jdbjj status              # 进度/GPU/进程
python3 rmt.py jdbjj tail pipeline 20    # 日志尾部
python3 rmt.py jdbjj run "统计产物数量并输出表格"   # 交给远端 agent
python3 rmt.py jdbjj start|stop|package  # 幂等动作
```

---

## 9. 新机器 onboarding（8 步）

1. 本地建项目目录 + `.venv`，装依赖（同一套脚本可直接复制）
2. 生成**三个独立随机令牌**：`.sshpass`、`.agent_token`、`.reasonix_token`
3. 部署 `scripts/`：`reverse_tunnel.py`、`local_control_server.py`、`agent_daemon.py`、
   `supervisor.py`、`watchdog.py`、`watchdog_transcribe.py`、`watchdog_tunnel.py`、`_singleton.py`
4. 给所有长驻脚本注入**单实例守卫**（端口按 §4 表格顺延，见 skill §4.4）
5. 用 **CIM 方式**启动 `supervisor.py`（否则被会话回收，见 skill §4.6）
6. 云端写 `/root/machines/<name>/env.sh`（令牌、`JDBJJ_REMOTE`），登记 `machines.json`
7. 校验：`python3 rmt.py <name> status` 返回该机主机名与进度
8. 挂云端巡检 cron（见 §11），`deliver=origin`，静默模式

---

## 10. 长任务可靠性设计（摘要，细节见 skill §4）

| 风险 | 机制 |
|---|---|
| **看门狗把"慢"当"死"** | 进度文件（秒级）+ 动态阈值 `max(40, 段时长×1.5+10)` |
| **进度无处反馈** | 生产脚本在产出循环里 `beat()` 原子写 `run/*-progress.json`（限流 5 秒） |
| **进程重复繁殖** | 端口式单实例守卫 + `busy()` 双条件去重 |
| **起的进程被回收** | CIM `Win32_Process Create`（或 `schtasks`）拉起 supervisor |
| **误杀链式进程** | 保留写入者 + 祖先保护 + 承载长任务的进程不手杀 |
| **小机器雪崩** | swap + `flock` 互斥 + cron 错峰 + 负载自保 |
| **编码坑** | `encoding="utf-8", errors="replace"` + `PYTHONIOENCODING=utf-8` |
| **隧道断链** | `watchdog_tunnel.py` 自愈 + 本地计划任务探测 9000 端口 |

---

## 11. 可观测性与巡检

云端 cron（`no_agent`，有输出才推送）：

```
*/30 * * * *  jdbjj_progress.py      # 转录巡检：段进度判活、断线首发即报 + 每 2h 重申
```

- **判活**：产物数增加 **或** 流水线段进度变化 → 在干活
- **报卡住**：两者都停 ≥ 90 分钟
- **快照去重**：同状态 10 分钟内不重复
- **断线**：与卡住区分报告（避免"隧道断了"被误读成"任务卡了"）

---

## 12. 故障排查

| 现象 | 原因 | 处置 |
|---|---|---|
| `Failed to connect ... 9000/9020` | 隧道未建立 | 本地 `tunnel.ps1 status` → `restart`；查 `watchdog_tunnel.py` 是否在跑 |
| `timed out` | 隧道在但本地服务未起 | 本地查控制面/serve；`supervisor.py` 应会拉起 |
| 9000 返回**纯文本 401** | 端口转发到了错后端（paramiko 单 handler 槽位被覆盖） | 确认 `reverse_tunnel.py` 用统一 handler + `_PORT_MAP` |
| `{"ok": false, "error": "令牌不匹配"}` | 令牌不对 | `source env.sh` 重取 |
| `{"ok": false, "error": "非白名单动作"}` | 动作名不在表内 | 见 §6 |
| `rc=0` 但 `output` 为空 | GBK 编码坑（只有中文/工具任务失败） | 见 skill §4.9 |
| 计数长时间不变 | **可能正常**（长分片） | 看进度文件 `pct` 两采样；别急着重启 |
| 进程重复 | `busy()` 判定失效 / 老实例无守卫 | 见 skill §4.4/§4.5；一次性重启收敛 |

---

## 13. 安全模型与加固路线

**现状**：云端 SSH 凭据（root）**等同于本地机器的控制权**（因 `reasonix` 动作接受任意任务）。

加固清单（按优先级）：

1. **SSH 换密钥认证**，关闭密码登录
2. 为本用途建**单独非 root 账号**，仅授权项目目录
3. **每机独立令牌**，云端按机分目录存放，权限 `600`
4. `reasonix` 动作**全量审计**（`logs/agent-audit.log` 记录任务与结果摘要）
5. 组转发的端口**永远只绑 `127.0.0.1`**（改 `0.0.0.0` = 把 agent 控制面暴露公网）
6. 失败登录监控（曾有 `x.x.x.x`、`y.y.y.y` 暴力破解尝试）

---

## 14. 现状快照（2026-10-12）

| 项 | 值 |
|---|---|
| 本机主机名 | `MACHINE-A` |
| 项目 | `C:\path\to\project` |
| 任务 | 262 个音频分片 → 转录（large-v3 / faster-whisper） |
| 进度 | **240 / 262（91.6%）**，30 部教学片，剩余 ≈ 9 小时 |
| 算力 | GPU 98% / 温度 51–52°C / 磁盘 240.5GB |
| 已修 | 活锁（看门狗误杀长分片）、进度反馈缺失、重复实例（守卫 + `busy()`） |
| 未收敛 | 补丁前启动的老实例（`transcribe/tunnel/control` 各 2 份），待本地一次性重启 |

---

## 15. 演进路线

1. **多机看板**：`rmt.py list` 聚合所有机器状态，云端一个页面（或每日推送）看全部
2. **统一巡检器**：把 `jdbjj_progress.py` 泛化为读 `machines.json` 的多机巡检
3. **下发式运维**：把补丁脚本收进云端 `patches/`，按机器名批量投送 + 自动验证
4. **凭据**：密钥认证 + 非 root + 每机令牌轮换
5. **任务编排**：队列化（`queue/` + `done/`）驱动的跨机任务分发
