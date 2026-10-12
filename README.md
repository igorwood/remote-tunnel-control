# Remote Tunnel Control · 云端反向隧道远程控制套件

> 让**云端机器**远程查看并控制位于 NAT 后（无公网 IP）的**多台本地机器**，
> 并保证跑几小时到几天的长任务 **跑得住、不误杀、不重复**。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey.svg)]()
[![Guides](https://img.shields.io/badge/Docs-4%20guides-informational.svg)](docs/)
[![Skill](https://img.shields.io/badge/Hermes-Agent-Skill-purple.svg)](skills/)

---

## 这是什么

一套**经过实战验证**的"云端 → 多台本地机器"远程控制方案，包含：

- **架构与协议**：一条 SSH 反向隧道 + 两个 HTTP 接触面（白名单控制面 / agent 任意任务面）
- **可复用代码**：单实例守卫、多机统一 CLI、确定性投送器
- **可靠性方法论**：长任务"活性判定 + 动态阈值 + 进程去重"的完整做法（本仓库的精华）

它不绑定具体业务：**转录、爬取、渲染、批处理、模型训练、跨机数据同步**……任何
"跑很久、在 NAT 后、需要远程看进度"的任务都适用。

## 为什么需要它

| 痛点 | 本方案的做法 |
|---|---|
| 本地机器在 NAT 后，云端**无法主动连接** | 由本地主动建 **SSH 反向隧道**，两端只绑回环，云端零对外开放端口 |
| 长任务跑几小时，"**看不见进度**" | 生产循环里写**秒级进度心跳**（原子写），控制面随时读 |
| 通用看门狗按"**有没有新产物**"判活 → 长任务被反复误杀 | 判活改看**秒级心跳**，阈值**随任务单元时长动态放大**（`max(40, 时长×1.5+10)` 分钟） |
| 守护进程**重复繁殖**，互相踩 | **端口式单实例守卫**（自愈、无陈锁）+ 双条件去重判定 |
| 从远程会话起的常驻进程**随会话消失** | 用 **WMI/CIM**（或计划任务）拉起，脱离会话作业对象 |
| 重启后**隧道断了没人管** | 专用隧道看门狗自动重建；云端巡检区分"**卡住**"与"**断线**" |

## 架构：只有三个接触面

```
┌──────────────── 云端（控制方）─────────────────┐
│ 控制面客户端 · 巡检器 · 投送器 · 多机注册表      │
│ 隧道出口 127.0.0.1:9000（控制面）               │
│ 隧道出口 127.0.0.1:9020（agent）                │
└───────────────────────┬────────────────────────┘
        SSH 反向隧道（本地主动发起，仅回环）
┌───────────────────────┴────────────────────────┐
│ 隧道 · 控制面 · agent 运行时                    │
│ 编排（supervisor/watchdog）· 长任务流水线        │
│ 状态文件 · 单实例守卫                           │
└─────────────────────────────────────────────────┘
```

1. **SSH 反向隧道** —— 传输
2. **HTTP 控制面** —— 白名单动作（状态/日志/启停）+ `agent` 任意任务
3. **HTTP agent 运行时** —— 官方接口（可换成任意支持 HTTP 的本地 agent）

> 详细的双端程序清单与对接协议见 **[`docs/01-interface-contract.md`](docs/01-interface-contract.md)**

## 快速开始

```bash
# 0) 本地机器：放好令牌文件、部署脚本、给长驻脚本注入单实例守卫
# 1) 本地机器：用 CIM/计划任务拉起 supervisor（切勿用会话内子进程）
# 2) 云端：登记该机器

# 多机统一 CLI
python3 rmt.py list                     # 所有机器 + 在线状态
python3 rmt.py status  machine-a        # 进度 / GPU / 进程 / 日志尾
python3 rmt.py tail    machine-a pipeline 20
python3 rmt.py run     machine-a "统计产物数量并输出表格"
python3 rmt.py start | stop | package   machine-a

# 确定性投送补丁（改远端代码的标准姿势）
python3 push_remote_file.py machine-a ./patch_x.py scripts/patch_x.py
```

从零接线（含端口分配、令牌、守卫、验收口径）见
**[`docs/02-architecture.md`](docs/02-architecture.md)** 的 onboarding 清单。

## 核心可靠性机制

1. **别把"慢"当"死"** —— 判活只认真进度：`run/*-progress.json` 新鲜度 + 动态阈值
2. **一个组件只准一个实例** —— 端口式单实例守卫；去重判定 = `精确命令行片段 ∪ 守卫端口`
3. **常驻进程用 CIM/计划任务拉起** —— 会话内子进程会随会话被回收
4. **杀进程前先保命** —— 保留"正在写入状态的实例"、保护其祖先链（链式自派生的坑）
5. **编码统一 UTF-8** —— 否则 Windows 子进程输出会静默变空（且只有中文任务失败）
6. **资源自保** —— swap + `flock` 互斥 + 定时任务错峰，避免小机器雪崩

> 12 条完整清单（每条带**根因与硬证据**）见
> **[`docs/04-skill.md`](docs/04-skill.md)** · 活性判定实现要点见
> **[`docs/03-liveness-watchdog.md`](docs/03-liveness-watchdog.md)**

## 目录结构

```
remote-tunnel-control/
├── README.md                          本文件
├── LICENSE
├── docs/
│   ├── 01-interface-contract.md       双端程序清单与对接协议（先读这个）
│   ├── 02-architecture.md             拓扑 · 多机端口规划 · API 参考 · onboarding
│   ├── 03-liveness-watchdog.md        活性判定与动态阈值实现要点
│   ├── 04-skill.md                    防崩溃/防误杀/防重复全集（12 条）
│   └── remote-tunnel-control-suite.zip 资料包（上述文档 + 脚本打包）
└── skills/
    └── remote-machine-tunnel-control/ 可安装的 Skill（丢进 ~/.hermes/skills/ 即可用）
        ├── SKILL.md
        ├── references/{architecture,interface-contract}.md · machines.example.json
        └── scripts/{_singleton.py, rmt.py, push_remote_file.py}
```

## 适用场景

- **长时任务**：语音转录、视频转码、爬虫、批量渲染、模型训练/推理、跨机同步
- **NAT 后的机器**：家里/办公室的 GPU 机器、边缘设备、无公网 IP 的服务器
- **一人多机**：一台云端做主控，多台本地机器跑任务并统一观测

## 安全模型（务必阅读）

- 本方案把"**本地 agent 的控制权**"交给云端：**云端的访问凭据 ≈ 本地机器的控制权**
- 生产建议：**密钥认证**（关闭密码登录）、**单独非 root 账号**、**每机独立令牌**、
  **全量审计**（每次 agent 调用都写日志）、转发端口**永远只绑 `127.0.0.1`**
- 仓库内的示例已完全脱敏（主机名 / 用户名 / 路径 / IP / 令牌均为占位符）

## English Summary

A battle-tested toolkit for controlling machines behind NAT from a cloud host over a
single SSH reverse tunnel, exposing exactly two HTTP surfaces (a whitelisted control
plane and an agent runtime). It ships the reusable pieces — a **port-based single-instance
guard**, a **multi-machine CLI**, and a **deterministic file-push runner** — plus the
methodology that makes long-running jobs survive unattended operation: **liveness from
second-level progress heartbeats instead of artifact counts**, **dynamic stall thresholds**,
**process de-duplication**, and **session-independent process spawning**. Domain-agnostic:
transcription, scraping, rendering, batch jobs, training, sync — anything long-running
behind NAT that you need to observe and steer remotely.

## License

MIT — see [LICENSE](LICENSE).
