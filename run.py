#!/usr/bin/env python3
"""服务入口：python run.py --port <PORT>（默认 5000，监听 0.0.0.0）。

环境变量：BAIDU_MAP_AK（必填，从 lbsyun.baidu.com 申请）。
"""
import argparse

import uvicorn

from baidu15min import config
from baidu15min.api.app import app


def main():
    parser = argparse.ArgumentParser(description="15分钟便民生活圈体检报告 v4 (FastAPI)")
    parser.add_argument("--port", type=int, default=5000, help="监听端口（默认 5000）")
    args = parser.parse_args()
    print("15分钟便民生活圈服务 v4 (FastAPI/uvicorn) 启动，监听 0.0.0.0:%d，ak_configured=%s"
          % (args.port, bool(config.BAIDU_AK)))
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()