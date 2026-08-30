"""Entry point: `python -m c4x.cli`.

Argument parsing only. The work is in commands.py, so a broken command is one small file to read
rather than a script that also happens to define the whole program.
"""
import argparse
import sys

from c4x.cli import commands


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m c4x.cli",
        description="The dashboard's data, without the dashboard.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tabs", help="list the tab ids")

    p_sessions = sub.add_parser("sessions", help="list sessions, newest first")
    p_sessions.add_argument("--limit", type=int, default=20)
    p_sessions.add_argument("--json", action="store_true")

    p_dump = sub.add_parser("dump", help="render one tab and print its data")
    p_dump.add_argument("--tab", required=True)
    p_dump.add_argument("--session", default=None)
    p_dump.add_argument("--scope", default="main", choices=["main", "all"])
    p_dump.add_argument("--cohort", default=None)
    p_dump.add_argument("--compare-with", default=None,
                        help="tab-compare only: the other arm")
    p_dump.add_argument("--compare-kind", default="session", choices=["session", "cohort"])
    p_dump.add_argument("--json", action="store_true")

    p_all = sub.add_parser("all", help="render every tab and report what each produced")
    p_all.add_argument("--session", default=None)
    p_all.add_argument("--scope", default="main", choices=["main", "all"])
    p_all.add_argument("--cohort", default=None)
    p_all.add_argument("--verbose", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    handler = {
        "tabs": commands.cmd_tabs,
        "sessions": commands.cmd_sessions,
        "dump": commands.cmd_dump,
        "all": commands.cmd_all,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
