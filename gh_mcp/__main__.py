"""Command-line entry point: ``gh-mcp --config gh-mcp.toml``."""

import argparse
import socket
from pathlib import Path

from aiohttp import web
from systemd import daemon

from .config import load_config
from .server import build_app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gh-mcp", description="Policy-gated GitHub MCP server"
    )
    parser.add_argument("--config", type=Path, required=True, help="TOML confi file")
    args = parser.parse_args()
    cfg = load_config(args.config)
    fds = daemon.listen_fds()
    if len(fds) != 1:
        raise RuntimeError(f"expected exactly one socket from systemd, got {len(fds)}")
    if not daemon.is_socket_unix(fds[0], type=socket.SOCK_STREAM, listening=1):
        raise RuntimeError(f"fd {fds[0]} is not a listening Unix stream socket")
    web.run_app(
        build_app(cfg),
        sock=socket.socket(fileno=fds[0]),
        handler_cancellation=True,
    )


if __name__ == "__main__":
    main()
