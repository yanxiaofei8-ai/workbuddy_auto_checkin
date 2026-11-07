#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 签到助手 · 统一入口

在用户显式配置长效令牌后，执行每日签到与 Buddy 旅行。
令牌由用户自行录入，脚本仅使用该令牌请求官方接口。

命令行：
  python auto_checkin.py                  # 签到 + Buddy 旅行闭环（默认）
  python auto_checkin.py --check          # 只查询状态
  python auto_checkin.py --no-travel      # 只签到
  python auto_checkin.py --setup-rt '<令牌>'   # 录入令牌
  python auto_checkin.py --forget         # 清除本机保存的令牌
  python auto_checkin.py --version        # 显示版本
  python auto_checkin.py --help           # 显示帮助
"""

import json
import os
import sys

VERSION = "3.1.8"
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import rt_auth
except Exception:  # 模块缺失时不崩溃，由 main 给出提示
    rt_auth = None


def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print(json.dumps({"version": VERSION}))
        return 0
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if rt_auth is None:
        sys.stderr.write("签到执行模块缺失，请确认 scripts/ 目录文件完整。\n")
        return 1

    old_argv = sys.argv[:]
    sys.argv = [getattr(rt_auth, "__file__", "rt_auth.py")] + list(argv)
    try:
        rc = rt_auth.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    except Exception as e:
        sys.stderr.write("执行异常：%s\n" % e)
        rc = 1
    finally:
        sys.argv = old_argv
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
