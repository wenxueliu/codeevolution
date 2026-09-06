#!/usr/bin/env python3
"""Build and start the CodeEvolution Web service with one command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from scripts import service


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: %(default)s)")
    parser.add_argument("--no-build", action="store_true", help="Skip the frontend build")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = create_parser().parse_args(argv)
    try:
        service.start(args.host, args.port, should_build=not args.no_build)
    except RuntimeError as error:
        raise SystemExit(f"[codeevolution] error: {error}") from None


if __name__ == "__main__":
    main()
