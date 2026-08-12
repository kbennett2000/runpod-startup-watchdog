"""Command-line entry point.

Skeleton only: --version works, nothing else does yet.
"""

from __future__ import annotations

import argparse

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runpod-watchdog",
        description="Watch one new Runpod pod during startup and stop it if it never becomes healthy.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
