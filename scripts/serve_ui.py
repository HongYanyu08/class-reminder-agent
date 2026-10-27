#!/usr/bin/env python3
"""Start the local web configuration UI.

Usage:
  python3 scripts/serve_ui.py
  python3 scripts/serve_ui.py --port 8766 --data-dir /tmp/my-data

The server binds 127.0.0.1 by default and has no authentication. It is a
single-user local tool: do not expose it on a LAN or the public internet.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.webui import run_server

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="上课提醒本地 Web 配置 UI")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认 127.0.0.1，仅本机）")
    parser.add_argument("--port", type=int, default=8765,
                        help="监听端口（默认 8765）")
    parser.add_argument("--data-dir", default=str(ROOT / "data"),
                        help="数据目录（默认项目 data/）")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, data_dir=Path(args.data_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
