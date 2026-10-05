#!/usr/bin/env python3
"""启动钢铁产能改造核证后端 HTTP 服务。

用法：
    python run_server.py [--db steel_audit.db] [--host 127.0.0.1] [--port 8080]

默认使用 SQLite 文件库 steel_audit.db；传 --db :memory: 使用内存库（重启即清空）。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from steel_audit.api import make_server
from steel_audit.service import SteelAuditService
from steel_audit.storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="钢铁产能改造核证后端")
    parser.add_argument("--db", default="steel_audit.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    service = SteelAuditService(Store(args.db))
    server = make_server(service, args.host, args.port)
    print(f"钢铁产能改造核证后端已启动: http://{args.host}:{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
