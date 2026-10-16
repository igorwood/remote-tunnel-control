# 代理侧运行时套件（agent-kit）

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
curl -s -X POST http://127.0.0.1:9090/agent -H 'Content-Type: application/json' \
     -d '{"task":"只回复 OK"}'
curl -s http://127.0.0.1:9090/ping
```
返回形如 `{"ok":true,"output":{"rc":0,"output":"OK","runtime":"…","adapter":"…","secs":0.8}}` 即合格。
