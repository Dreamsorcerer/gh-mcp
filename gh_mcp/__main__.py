"""Command-line entry point: ``gh-mcp --config gh-mcp.toml``."""

import argparse
from pathlib import Path

from aiohttp import web

from .config import DEFAULT_SOCK, load_config
from .server import build_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gh-mcp", description="Policy-gated GitHub MCP server"
    )
    parser.add_argument("--config", type=Path, required=True, help="TOML confi file")
    args = parser.parse_args()
    cfg = load_config(args.config)
    sock = cfg.get("server", {}).get("sock", DEFAULT_SOCK)
    web.run_app(build_app(cfg), path=str(sock), handler_cancellation=True)


if __name__ == "__main__":
    main()
