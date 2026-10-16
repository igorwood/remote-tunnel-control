# 新代理接入云端（端口分配 + 注册清单）

> 云端：`root@CLOUD_HOST`（自己的云主机）｜本机：你要接入的那台
> 目标：让你的机器与云端建立反向隧道，云端可通过控制面查看/操作你，并可用统一 CLI 管理

---

## 一、端口分配（**照这个用，不要自选**）

| 机器 | 云端隧道端口（控制面 / agent） | 本机单实例守卫端口段 | 状态 |
|---|---|---|---|
| #1 `MACHINE-A` | 9000 / 9020 | 19100–19110 | 已占用（在用） |
| **#2 你（本次接入）** | **9100 / 9120** | **19200–19210** | ← 分配给你 |
| #3（预留） | 9200 / 9220 | 19300–19310 | 未使用 |

约定：**隧道端口 = 9000 + 100×序号**；**守卫端口段 = 19100 + 100×序号**。
两个隧道端口都只绑云端的 `127.0.0.1`（外网扫不到），你这边也全部只绑本机回环。

---

## 二、你本机要做的事（按顺序）

### 1) 目录与依赖
- 项目目录示例：`C:\Users\<你>\Projects\<你的项目>`
- 建 venv 并装依赖：**paramiko（必需）**、其余按需

### 2) 三个令牌文件（**每机独立，禁止复用 #1 的**）
放到 `scripts\` 下，各一行、无多余空白：
| 文件 | 用途 | 生成方式 |
|---|---|---|
| `scripts\.sshpass` | 云端 SSH 密码（你应已从管理员处获得） | 由管理员提供 |
| `scripts\.agent_token` | 控制面令牌（HTTP 头 `X-Token`） | `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `scripts\.reasonix_token` | agent serve 令牌（`Authorization: Bearer`） | 同上，另生成一个 |

> 更稳妥：**改用 SSH 密钥认证** —— 你本机生成密钥对，把公钥交给管理员加到云端
> `~/.ssh/authorized_keys`，之后隧道与上传都不用密码。

### 3) 部署脚本（从 #1 复制或向管理员索取）
`reverse_tunnel.py`、`local_control_server.py`、`agent_daemon.py`、`supervisor.py`、
`watchdog.py`、`watchdog_tunnel.py`、`_singleton.py`（单实例守卫）、`sftp_put.py`、`ssh_run.py`

### 4) 注入单实例守卫（端口用**你的** 19200 段，按脚本偏移）

| 偏移 | 脚本 | 端口 |
|---|---|---|
| +0 | `supervisor.py` | 19200 |
| +1 | `watchdog.py` | 19201 |
| +2 | `watchdog_transcribe.py` | 19202 |
| +3 | `watchdog_tunnel.py` | 19203 |
| +4 | `run_pipeline.py` | 19204 |
| +5 | `transcribe.py` | 19205 |
| +6 | `reverse_tunnel.py` | 19206 |
| +7 | `local_control_server.py` | 19207 |
| +8 | `agent_daemon.py` | 19208 |
| +9 | `monitor.py` | 19209 |
| +10 | `package_and_upload.py` | 19210 |

在每个脚本的 `if __name__ == "__main__":` **之前**插入（把名字/端口换成上表对应值）：
```python
try:
    import _singleton                                    # 单实例守卫（防重复实例）
    if not _singleton.acquire("supervisor", 19200):
        raise SystemExit(0)
except ImportError:
    pass
```
> 为什么必须加：这类守护脚本会被反复拉起，没有守卫就会**重复繁殖**、互相踩。

### 5) 起隧道（**关键：map 到 9100 / 9120**）
```powershell
.venv\Scripts\python.exe scripts\reverse_tunnel.py --map 9100:9101 --map 9120:9120
```
- `9100:9101` = 云端 127.0.0.1:9100 → 你本机控制面 127.0.0.1:9101
- `9120:9120` = 云端 127.0.0.1:9120 → 你本机 agent serve 127.0.0.1:9120
- 按你的实际本地端口改右侧数字即可；**左侧必须是 9100 / 9120**

### 6) 起两个本地服务
```powershell
python scripts\local_control_server.py --port 9101          # 控制面（认证头 X-Token）
reasonix-cli.exe serve --addr 127.0.0.1:9120 --auth token    # agent serve（Bearer）
```

### 7) 用 CIM 起 supervisor（**别用会话内子进程**，任务结束会被一并回收）
```powershell
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine='"<venv 的 python.exe>" "<项目根>\scripts\supervisor.py" --interval 300';
  CurrentDirectory='<项目根>'}
```
返回 `ReturnValue=0` 即成功；它会保证各组件**有且只有一个**在跑。

### 8) 自测
```powershell
python -c "import json,urllib.request;print(json.loads(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:9101/ping',headers={'X-Token':open('scripts/.agent_token').read().strip()})).read()))"
```
预期：`{'ok': True, 'action': 'ping', ...}`

---

## 三、回执给管理员（我），我来做云端注册与验收

请回复以下 5 项（**不要贴任何令牌明文**）：
1. 本机主机名
2. 控制面本地端口（示例 9101）与 serve 本地端口（示例 9120）
3. 三个令牌文件是否就绪（是/否）
4. `/ping` 自测结果（原文）
5. 是否已加单实例守卫（是/否）

**云端侧注册（管理员执行，你不用管）**：
```json
// /opt/project/machines.json 追加
"machine2": {
  "desc": "第 2 台 · <用途>",
  "control_port": 9100, "serve_port": 9120,
  "env": "/opt/machines/machine2/env.sh", "token_key": "MACHINE_B_TOKEN",
  "guard_base": 19200
}
```
```bash
mkdir -p /opt/machines/machine2
printf 'MACHINE_B_TOKEN=%s\n' "<你给我的令牌>" > /opt/machines/machine2/env.sh
chmod 600 /opt/machines/machine2/env.sh
python3 /opt/project/rmt.py list          # 应出现两台且都「在线」
python3 /opt/project/rmt.py machine2 status
```

---

## 四、安全红线（务必遵守）

1. 隧道端口**只绑 `127.0.0.1`**；**不要**改 `0.0.0.0`、不要开 `GatewayPorts`
2. **每机独立令牌**，不得复用 #1 的
3. 云端 SSH 凭据不要写入任何会提交到 Git 的文件
4. 加了 `agent serve` 就等于把本机 agent 控制权交给云端 → 建议尽快换**密钥认证**并给该用途单独账号

---

## 五、如果你的"第二个代理"跑在**同一台机器**上（另一变体）

那**不需要新隧道**，只要：
1. 新起一个 serve 实例监听 `9021`，用**独立令牌**
2. 在现有 `reverse_tunnel.py` 的启动参数里**追加一条映射**：`--map 9121:9021`
3. 云端注册为 `machine2`，`control_port=9121`（如果它也有自己的控制面，则给它 `9110` 段）

→ 这种情况回我一句"同机第二实例"，我给你对应的端口与命令。
