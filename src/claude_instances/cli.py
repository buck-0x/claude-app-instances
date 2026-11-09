"""Command-line entry point for claude-instances."""

import argparse
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
    # Own dest so the subparser's value and the top-level --key-fd never overwrite each other.
    probe.add_argument("--key-fd", dest="probe_key_fd", type=int, required=True, metavar="INT")
    probe.add_argument("--data-dir", type=Path, required=True, metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "probe":
        from claude_instances.httpclient import UrllibHttpClient
        from claude_instances.probe import run_probe

        return run_probe(Path(args.data_dir), args.probe_key_fd, UrllibHttpClient())
    print("TUI not wired yet")
    return 0
