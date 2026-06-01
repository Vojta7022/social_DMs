from __future__ import annotations

import argparse

import uvicorn


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local archive viewer for social-scanner."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for the local viewer. Default: 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for the local viewer. Default: 8000",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    uvicorn.run(
        "src.viewer:app",
        host=args.host,
        port=args.port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
