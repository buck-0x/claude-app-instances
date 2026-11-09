"""Command-line entry point for claude-instances."""

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-instances",
        description="Manage multiple Claude desktop app instances.",
    )
    parser.add_argument(
        "--key-fd",
        type=int,
        metavar="INT",
        help="file descriptor to read the Safe Storage key from",
    )
    parser.add_argument(
        "--apps",
        type=Path,
        action="append",
        metavar="PATH",
        help="directory to scan for launchers (repeatable; replaces the default scan dirs)",
    )
    sub = parser.add_subparsers(dest="command")
    probe = sub.add_parser("probe", help="probe one instance's data dir")
    probe.add_argument("--key-fd", type=int, required=True, metavar="INT")
    probe.add_argument("--data-dir", type=Path, required=True, metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "probe":
        print("not implemented", file=sys.stderr)
        return 2
    print("TUI not wired yet")
    return 0
