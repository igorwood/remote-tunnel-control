# 云端 ↔ 本地 反向隧道控制方案 · 资料包

> 版本：2026-10-12 ｜ 首个落地实例：`DESKTOP-CKQ4CLB`（Windows · JDBJJ 转录流水线）
> 用途：让云端机器远程查看/控制 NAT 后的多台本地机器，并保证长任务**跑得住、不误杀、不重复**。

---

## 阅读顺序

| 顺序 | 文件 | 内容 |
|---|---|---|
| ① | `01-interface-contract.md` | **双端程序清单与对接协议**：本地端 14 个程序、云端端 8 类程序、三个接触面、HTTP 契约、三条交互时序、从零接线 8 步握手 |
| ② | `02-architecture.md` | 拓扑与多机规划：端口分配规范、组件启动链与职责、控制面 API 参考、演进路线 |
| ③ | `04-SKILL.md` | **防崩溃 / 防误杀 / 防重复 · 解决方案全集**（12 条，含根因与硬证据）＋ 云端调用范式 ＋ 验证清单 ＋ 故障速查 ＋ 安全模型 |
| ④ | `03-liveness-watchdog.md` | 活性判定实现要点：进度心跳怎么写、动态阈值公式、日志自证标准 |

## 目录结构

```
remote-tunnel-control/
├── README.md                    ← 本文件
├── 01-interface-contract.md     ← 双端程序与对接协议（先读这个）
├── 02-architecture.md           ← 拓扑与多机规划
├── 03-liveness-watchdog.md      ← 活性判定/动态阈值
├── 04-SKILL.md                  ← 防崩溃全集的 Skill 主文档
└── scripts/                     ← 可复用代码（拷进各机器 scripts/ 即可用）
    ├── _singleton.py            ← 端口式单实例守卫
    ├── rmt.py                   ← 云端多机统一 CLI
    ├── push_remote_file.py      ← 云端「确定性投送脚本到远端」投送器
    └── machines.example.json    ← 多机注册表模板
```

## 一分钟上手（云端）

```bash
# 1) 令牌（内容不回显）
source /root/jdbjj/env.sh

# 2) 多机 CLI
python3 /root/jdbjj/rmt.py list                      # 所有机器 + 在线状态
python3 /root/jdbjj/rmt.py status jdbjj              # 进度 / GPU / 进程 / 日志尾
python3 /root/jdbjj/rmt.py tail   jdbjj pipeline 20  # 日志
python3 /root/jdbjj/rmt.py run    jdbjj "统计产物数量并输出表格"
python3 /root/jdbjj/rmt.py start|stop|package jdbjj  # 幂等动作

# 3) 投送补丁（改远端代码的标准姿势）
python3 /root/jdbjj/push_remote_file.py jdbjj /root/jdbjj/_patch_x.py scripts/_patch_x.py
```

## 三条铁律（血泪总结）

1. **别把"慢"当"死"**：判活看**秒级进度心跳**（`run/*-progress.json`），不看"有没有新产物"；
   阈值动态化 `max(40, 任务段时长分钟 × 1.5 + 10)`。
2. **一个组件只准一个实例**：端口式单实例守卫（自带自愈、无陈锁）；去重判定用
   `精确命令行片段 ∪ 守卫端口`（PowerShell 的命令行匹配会失效，不能单靠它）。
3. **从远程会话起的常驻进程会随会话被回收**：用 **CIM `Win32_Process Create`**（或 `schtasks`）拉起。

## 现状快照（2026-10-12）

- 转录任务 **240 / 262 分片（91.6%）**，30 部教学片，剩余约 9 小时
- 已修：活锁（看门狗误杀 82.5 分钟长分片）、无进度反馈、重复实例繁殖
- 待收敛：补丁前启动的老实例（需本地一次性重启）
