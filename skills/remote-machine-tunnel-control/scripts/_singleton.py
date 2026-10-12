#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单实例守卫：同一脚本只允许一个实例存活（端口式，自愈、无陈锁）。

用法（放在脚本末尾 `if __name__ == "__main__":` 之前）：

    try:
        import _singleton
        if not _singleton.acquire("watchdog_transcribe", 19102):
            raise SystemExit(0)
    except ImportError:
        pass

约定：端口按机器编号分段（见 references/architecture.md §4）
      DESKTOP-CKQ4CLB = 19100+，MACHINE-B = 19200+，MACHINE-C = 19300+
"""
import socket
import sys

_kept = {}          # 保持 socket 引用，进程存活期间不释放


def acquire(name, port, tries=3, gap=2.0):
    """拿到独占 → True；已有实例 → False（调用方应直接退出）。

    tries/gap：绑定失败时重试几次，容忍"父进程正在退出、子进程刚起来"的交接竞态。
    """
    import time as _t
    last_err = None
    for i in range(max(1, tries)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind(("127.0.0.1", int(port)))
            s.listen(1)
        except OSError as e:
            last_err = e
            try:
                s.close()
            except Exception:
                pass
            if i < tries - 1:
                _t.sleep(gap)
            continue
        _kept[name] = s                 # 关键：持有引用，防止 socket 被回收
        print("[singleton] %s 已获得单实例锁（端口 %s）" % (name, port), flush=True)
        return True
    print("[singleton] %s 已有实例在运行（端口 %s 被占: %s）→ 本进程退出"
          % (name, port, last_err), flush=True)
    return False


if __name__ == "__main__":
    sys.exit(0 if acquire("_selftest", 19199) else 1)
