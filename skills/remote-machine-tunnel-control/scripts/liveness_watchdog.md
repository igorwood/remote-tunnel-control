# 活性判定 + 动态阈值：实现要点（防"把慢当死"）

## 一、两条信号线的区别

| 信号 | 粒度 | 来源 | 用途 |
|---|---|---|---|
| **产物计数** | 分钟~小时级 | 整片写完后更新的 `index.json` / 输出文件 | 总进度、对外汇报 |
| **段进度 / 进度文件** | **秒级** | 生产循环里 `beat()` 原子写 | **判活（慢 vs 死）** |

> 判据：**"产物计数"是粗略信号，"段进度"才是心跳。** 只用前者判活 → 长任务必被误杀。

## 二、生产侧：写出心跳（`beat`）

三个调用点 + 三条纪律：

```python
beat(rel, 0.0, dur_s, 0, state="start")          # ① 开始（顺带写下 total_s，供动态阈值用）
for s in segments:                                # ② 循环内每次产出（真进度；生成器=边解码边产出）
    beat(rel, s.end, dur_s, len(segs))
beat(rel, dur_s, dur_s, len(segs), state="done")  # ③ 完成
```

纪律：
1. **原子写**：写 `.tmp` → `os.replace()`（读端永远读不到半个文件）
2. **限流**：运行中每 5 秒最多写一次（避免长任务产生上千次 IO）；`start`/`done` 不受限流
3. **绝不抛异常**：整段 `try/except: pass` —— 进度写失败不能影响主任务

## 三、监控侧：判活

```python
def read_progress(path):
    """返回 dict（含 pct/part/total_s/state/fresh_min）；读不到返回 {}"""
    try:
        st = os.stat(path)
        d = json.load(open(path, encoding="utf-8"))
        d["fresh_min"] = (time.time() - st.st_mtime) / 60
        return d
    except Exception:
        return {}

prog = read_progress(PROGRESS)
alive = prog.get("fresh_min") is not None and prog["fresh_min"] < 3      # 3 分钟内刷新过 = 健康
total_s = prog.get("total_s") or 0
stall_min = max(40, int(total_s / 60 * 1.5) + 10)                        # 动态阈值
```

判定顺序：

1. **有真进度**（`fresh_min < 3`）→ `OK`，**什么都不做**（哪怕产物计数几小时没动）
2. 否则退化为产物级判定，但用**动态阈值** `stall_min`
3. `产物年龄 ≥ stall_min` **且** 进度文件也不新鲜 → 才重启
4. 记录的 `pid` 不存在 → 立即重启（真崩溃兜底，不受阈值约束）

## 四、动态阈值公式

```
stall_min = max(40, ceil(该任务单元时长(分钟) × 1.5) + 10)
```

- 依据：实测大模型转录 RTF ≈ 0.5–0.6（1 分钟音频 ≈ 0.5–0.6 分钟算力），
  再叠加模型加载/解码/IO，取 1.5 倍 + 10 分钟余量
- 例：82.5 分钟的长分片 → 134 分钟余量（旧阈值 40 分钟 → 每 40 分钟误杀一次 → 活锁）

## 五、日志必须自证

```
[19:02:32] OK      转录正常 157/258  真进度 98.4%（...P04.m4s）  产物 40 分钟前  进程 1
[19:43:05] RESTART 转录真卡死（产物 95 分钟前，进度文件 95 分钟未更新，阈值 134 分钟，进度 157/258）→ …
```

**验收标准**：改造后应出现"产物 40 分钟未变但仍判 OK"，且长分片一次跑完、RESTART 归零
（实测：改造后 **10.6 小时零 RESTART**）。

## 六、易错点

- ⚠️ 某些库的 `transcribe()` **不支持 `callback=`**（faster-whisper 具体版本相关，会 `TypeError`）；
  改用"**产出循环内写进度**"最稳（生成器天然增量）
- ⚠️ 进度文件路径用**项目根相对路径**（`run/xxx-progress.json`），别用 cwd 相对路径（cwd 会被改）
- ⚠️ 阈值别只调大（治标）：改大阈值只是把误杀推迟，**根因是没有秒级心跳**
- ⚠️ 两个看门狗同时判活时会同时杀+重启 → 靠单实例守卫（§4.4）收敛
