"""Command-line entry point.

This cycle covers settings only: parse the flags, layer them over the optional TOML config file,
validate the result, and print it. No network calls, no watching yet.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__, config
from .config import ConfigError, Settings

# Exit code for a settings problem. 2 is argparse's own code for a bad command line, so a bad flag
# and a bad config file report the same way.
EXIT_CONFIG_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runpod-watchdog",
        description=(
            "Watch one new Runpod pod during startup and stop it if it never becomes healthy. "
            "Settings come from these flags or from a TOML config file; a flag always wins."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="TOML file holding any of the settings below, using underscores in the names "
        "(pod_id, max_minutes, ...). Flags override whatever the file says.",
    )
    parser.add_argument(
        "--pod-id",
        default=None,
        help="The Runpod pod to watch. Required, from this flag or the config file.",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="How many minutes the pod gets to become healthy before the tool acts. Required, "
        "from this flag or the config file. Fractions are allowed.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Success signal: the pod is healthy once this TCP port answers.",
    )
    parser.add_argument(
        "--success-phrase",
        default=None,
        help="Success signal: the pod is healthy once this text appears in its log.",
    )
    parser.add_argument(
        "--failure-phrase",
        default=None,
        help="Failure signal: the pod is broken if this text keeps repeating in its log.",
    )
    # Every option defaults to None, including these three switches. That is what lets a config
    # file value survive when the matching flag is not typed; see config.merge and ADR-0002. The
    # paired --no-* forms are what make "flags override the file" true in both directions.
    parser.add_argument(
        "--retry",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Give the pod one more startup attempt before giving up. Default: off.",
    )
    parser.add_argument(
        "--terminate",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Delete the pod instead of stopping it. A stopped pod still accrues storage "
        "charges; a terminated pod is gone, along with anything on its disk. Default: off.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Report what would happen and change nothing. Default: off.",
    )
    return parser


def format_minutes(value: float) -> str:
    """10.0 prints as `10`; 0.5 prints as `0.5`."""
    return str(int(value)) if value == int(value) else str(value)


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _phrase(value: str | None) -> str:
    # Quoted so leading or trailing spaces in a phrase are visible when checking a run by eye.
    return "(not set)" if value is None else f'"{value}"'


def render(settings: Settings, config_path: str | None) -> str:
    """The settings report. Printing it is how a run is checked by eye: run the tool two ways
    and read which value won."""
    rows: list[tuple[str, str]] = [
        ("pod-id", settings.pod_id),
        ("max-minutes", format_minutes(settings.max_minutes)),
        ("port", "(not set)" if settings.port is None else str(settings.port)),
        ("success-phrase", _phrase(settings.success_phrase)),
        ("failure-phrase", _phrase(settings.failure_phrase)),
        ("retry", _yes_no(settings.retry)),
        ("terminate", _yes_no(settings.terminate)),
        ("dry-run", _yes_no(settings.dry_run)),
        ("config file", config_path if config_path else "(none)"),
    ]
    width = max(len(name) for name, _ in rows)
    lines = ["Settings for this run:"]
    lines += [f"  {name.ljust(width)}  {value}" for name, value in rows]
    lines += ["", "Watching is not implemented yet. This cycle covers settings only."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    flag_values = {name: getattr(args, name) for name in config.SETTING_NAMES}

    try:
        settings = config.resolve(flag_values, args.config)
    except ConfigError as exc:
        for line in exc.errors:
            print(f"error: {line}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    print(render(settings, args.config))
    return 0
