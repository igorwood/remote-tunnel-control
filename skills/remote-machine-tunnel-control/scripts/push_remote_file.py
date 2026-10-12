#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""push_remote_file.py —— 云端「投送器」：把本地文件原样送到远端机器执行。

这是改动远端代码的**标准姿势**（比让远端 agent 自由发挥可靠得多）：

    本地写好补丁脚本（锚点断言 + .bak 备份 + py_compile + 失败回滚 + 幂等）
      → 本脚本把文件内容嵌进 reasonix 任务
      → 远端 agent：原样保存 → 运行 → 完整回传输出 → 删除临时脚本（保留 .bak）
      → 我方核对 diff / 编译结果 / 关键行

用法：
    python3 push_remote_file.py <机器> <本地文件> [远端相对路径] [--no-run] [--timeout 240]

例：
    python3 push_remote_file.py jdbjj /root/jdbjj/_patch2.py scripts/_patch2.py

注意：
  * 任务里**必须**写明"原样照抄、完整回传、不要总结"，否则远端 agent 会自己改写/精简
  * 回传内容要**自己核对**（哈希/diff/编译结果），别信"已完成"这类自述
  * 单个任务别太长（>8KB 或 >5 步容易超时）→ 拆小
"""
import json
import os
import sys
import urllib.request

REG = "/root/jdbjj/machines.json"


def load_env(path):
    env = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                line = line[7:] if line.startswith("export ") else line
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    name, local = argv[1], argv[2]
    remote = argv[3] if len(argv) > 3 and not argv[3].startswith("--") else "scripts/" + os.path.basename(local)
    do_run = "--no-run" not in argv
    tmo = 240
    if "--timeout" in argv:
        tmo = int(argv[argv.index("--timeout") + 1])

    reg = json.load(open(REG, encoding="utf-8"))["machines"]
    if name not in reg:
        print("未知机器 %r（已注册：%s）" % (name, ", ".join(sorted(reg))))
        return 2
    m = reg[name]
    tok = load_env(m.get("env")).get(m.get("token_key", ""), "")
    code = open(local, encoding="utf-8").read()

    steps = ["1) 保存下面代码为 %s（utf-8，逐字符原样照抄，不要改写/格式化/加行号）" % remote]
    if do_run:
        steps.append("2) 运行：python %s（回传完整 stdout+stderr）" % remote)
        steps.append("3) 删除 %s（保留 .bak-* 备份）" % remote)
    else:
        steps.append("2) 只保存，不要运行")
    task = ("请按下面步骤做，把每步完整输出原样回传（**不要总结、不要省略**）：\n"
            + "\n".join(steps) + "\n\n===== CODE =====\n" + code + "\n===== END =====")

    body = json.dumps({"task": task, "mode": "print", "timeout": tmo}).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%s/reasonix" % m["control_port"], data=body,
                                 headers={"X-Token": tok, "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=tmo + 120).read().decode("utf-8", "ignore"))
    o = d.get("output", {})
    print("机器=%s  远端=%s  rc=%s  用时 %ss" % (name, remote, o.get("rc"), o.get("secs")))
    print("=" * 70)
    print(o.get("output") or json.dumps(d, ensure_ascii=False)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
