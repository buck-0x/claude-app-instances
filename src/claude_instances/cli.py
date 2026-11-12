"""Command-line entry point for claude-instances."""

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from claude_instances import paths

if TYPE_CHECKING:
    from claude_instances.backend import Backend

# The Claude app a launcher runs when its script names none.
DEFAULT_CLAUDE_APP = Path("/Applications/Claude.app")


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


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def build_backend(args: argparse.Namespace) -> "Backend":
    """The TUI backend: live usage when `--key-fd` yields a password, else cached only.

    Raises ValueError (fixed message, no secret) if the key fd is not UTF-8.
    """
    from claude_instances.actions import MacSystemActions
    from claude_instances.api import UsageLookup
    from claude_instances.backend import Backend
    from claude_instances.httpclient import UrllibHttpClient
    from claude_instances.secret import read_secret_fd

    lookup = None
    if args.key_fd is not None:
        password = read_secret_fd(args.key_fd)
        if password is not None:
            # UsageLookup derives the AES key from the password itself.
            lookup = UsageLookup(UrllibHttpClient(), password)
    return Backend(
        apps_dirs=args.apps if args.apps else paths.apps_dirs(),
        config_dir=paths.config_dir(),
        log_dir=paths.log_dir(),
        support_dir=paths.support_dir(),
        actions=MacSystemActions(),
        lookup=lookup,
        default_data_dir=paths.default_data_dir(),
        default_claude_app=DEFAULT_CLAUDE_APP,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "probe":
        from claude_instances.httpclient import UrllibHttpClient
        from claude_instances.probe import run_probe

        return run_probe(Path(args.data_dir), args.probe_key_fd, UrllibHttpClient())

    from claude_instances.app import InstancesApp

    try:
        backend = build_backend(args)
    except ValueError as e:
        print(f"claude-instances: {e}", file=sys.stderr)
        return 2
    app = InstancesApp(backend)
    try:
        app.run()
    finally:
        # A sign-in still running when the loop ends (SIGHUP teardown, an
        # exception) is cancelled here and its restore completes before exit.
        app.finish_signin()
    return 0
