# remote-tunnel-control

> 把 **NAT / 内网 / 沙箱** 后面的多台机器接进云端的**反向隧道控制面**：
> **一次性凭证注册** · **运行时无关适配** · **能力自动探测与画像** · **会话与文件台账** · 完整防崩溃方案集。
> 不依赖端口映射、不暴露公网端口、隧道端口只绑回环。

![License](https://img.shields.io/badge/License-MIT-green) ![Python](https://img.shields.io/badge/Python-3.9%2B-blue) ![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey) ![Docs](https://img.shields.io/badge/Docs-7%20docs-blue)

## 它解决什么

| 常见痛点 | 本方案的解法 |
|---|---|
| 目标机器在 NAT/内网/沙箱里，云端连不进去 | 目标机**主动**建反向隧道到云端，只绑 `127.0.0.1` |
| 手工登记 + 交换密钥，容易泄漏 | **凭证制注册**：云端签发一次性凭证 → 目标机持凭证兑换令牌（**不经过 SSH 传凭据**） |
| 每台机器的 agent 都不一样（Kimi / GLM / Claude / OpenAI / 自研 CLI） | **运行时适配层**：统一契约 + 适配器（cli_arg / cli_stdin / cli_template / openai_http / http_json） |
| 不知道远端 agent 支不支持某种调用 | **能力探测**：自动发现动作名、工具能力、延迟基准 → 写入**每机画像**，派活按画像走 |
| 只想知道"谁干了什么、传了什么文件" | **SQLite 台账**：`agents / sessions / messages / files / runtime_profiles / events` |

## 结构

```
.
├── enroll_service.py        注册服务（绑定 127.0.0.1，建议 systemd 常驻 + nginx /enroll 反代）
├── enroll.py                凭证管理 CLI（签发 / 列表 / 校验 / 注销）
├── enroll_client.py         目标机侧：用凭证兑换令牌
├── agent_adapters.py        运行时适配层（把任意 agent 归一为 run_task）
├── agent_bridge.py          任务桥（--task 执行；--self-test 输出能力卡片）
├── agent_action.py          统一动作服务（POST /agent，兼容 /task /run /reasonix）
├── agent_probe.py           云端探测/学习：动作发现 + 能力探测 + 延迟基准 → 每机画像
├── agentsdb.py              会话与文件台账（SQLite 单库）
├── rmt.py                   多机统一 CLI（list / status / tail / run / probe / profile）
├── push_remote_file.py      确定性投送脚本到远端并执行
├── runtime.example.json     运行时配置模板
├── docs/                    01 对接协议 · 02 架构 · 03 活性看门狗 · 04 Skill
│                            05 运行时适配与画像 · 06 新代理接入 · 07 凭证注册接入
├── ops/                     部署/打包/检查脚本
├── skills/                  可直接加载的 agent skill（含脚本）
└── tests/                   端到端测试（含反例）
```

## 快速开始

```bash
# 0) 云端：起注册服务（建议 systemd）+ nginx 反代 /enroll
python3 enroll_service.py &                 # 见 ops/enable_enroll_nginx.py

# 1) 云端：签发一次性凭证（自动打印可粘贴给目标机的整段说明）
python3 enroll.py issue --desc "渲染机" --control-port 9200 --serve-port 9220         --guard-base 19300 --machine-id render-1

# 2) 目标机：兑换令牌（自动写入 scripts/.agent_token）
python3 enroll_client.py --ticket RGT-XXXXX-XXXXX-XXXXX-XXXXX \
        --hostname %COMPUTERNAME% --control-port 9200 --serve-port 9220

# 3) 目标机：声明运行时 + 自测能力卡片
python3 agent_adapters.py --detect            # 扫描本机可用的 agent CLI / API key
python3 agent_bridge.py --self-test           # 生成能力卡片，交给云端

# 4) 云端：探测学习并写入画像
python3 agent_probe.py <machine>
python3 agent_probe.py --show

# 5) 云端：派活（自动按画像选择动作名与超时）
python3 rmt.py run <machine> "统计产物数量"

# 6) 台账
python3 agentsdb.py stats
python3 agentsdb.py sessions --machine <machine>
python3 agentsdb.py files --machine <machine>
```

## 统一动作契约（目标机侧要实现）

```
POST /agent      （推荐名；兼容别名 /task、/run）
{"task": "自然语言任务", "timeout": 180}
→ {"ok": true, "output": {"rc": 0, "output": "…", "runtime": "Kimi", "adapter": "cli_arg", "secs": 1.2}}
```
`rc=0` 成功 · `127` 命令不存在 · `124` 超时 · `501` 未启用任务通道 · 未知端点回 `404`（云端据此轮询候选动作）

## 可靠性设计（踩过的坑都写进文档了）

- **别把慢当死**：活性看真进度（进度文件/段进度），不看"有无新产物"；动态阈值 `max(40, 时长×1.5+10)`
- **单实例守卫**：每脚本独占绑定回环端口，进程死端口自动释放，无陈锁
- **完成态识别**：分母用"真实分片数"，别用"文件条目总数"（否则永远差点数 → 误报卡住）
- **链式派生杀进程雷区**、**CIM 持久拉起**、**小机器雪崩防护** 等 12 条见 `docs/03` 与 `skills/`

## 安全模型

- 隧道端口只绑 `127.0.0.1`；不修改 `GatewayPorts`
- 注册凭证：一次性 + 短 TTL + **绑定预留端口** + 限流 + 全量审计
- 每台机器**独立令牌**，只落本机文件；台账不索引密钥类文件
- 建议 SSH 用密钥认证 + 专用非 root 账号

## English summary

A reverse-tunnel control plane for machines behind NAT: **ticket-based enrollment** (no SSH
credential exchange), a **runtime-agnostic adapter layer** (Kimi / GLM / Claude / OpenAI / custom
CLI or HTTP), **automatic capability probing** that learns how to drive each agent and stores a
per-machine profile, plus a **SQLite ledger** of sessions and transferred files. Tunnel ports bind
to loopback only; enrollment tickets are single-use, TTL-bound and port-pinned.

## License

MIT
