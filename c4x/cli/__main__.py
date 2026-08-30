"""Entry point: `python -m c4x.cli`.

Argument parsing only. The work is in commands.py, so a broken command is one small file to read
rather than a script that also happens to define the whole program.
"""
import argparse
import sys

from c4x.cli import commands, source


def _backend_flags():
    """The source switch, shared by the top-level parser AND every subcommand.

    Two argparse behaviours make this less obvious than it looks, and both were hit here:

    - An option defined only on the top-level parser must appear BEFORE the subcommand.
      `c4x.cli all --via api` is what a person actually types, and it fails with
      "unrecognized arguments: --via api". Adding the flags to the subparsers too accepts both.
    - A subparser writes its DEFAULTS into the namespace after the main parser has already filled
      it, so `c4x.cli --via api all` would be silently reset to `dash` and would run against the
      wrong backend while printing no complaint at all. `SUPPRESS` is the fix: the attribute is
      only set when the flag is genuinely present, so nothing gets overwritten by a default.
    """
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--via", default=argparse.SUPPRESS, choices=[source.DASH, source.API],
                        help="read from the Dash app in this process, or from the API over HTTP")
    shared.add_argument("--api-url", default=argparse.SUPPRESS,
                        help=f"where the API is, when --via api (default {source.DEFAULT_URL})")
    return shared


def build_parser():
    shared = _backend_flags()
    parser = argparse.ArgumentParser(
        prog="python -m c4x.cli",
        parents=[shared],
        description="The dashboard's data, without the dashboard.")
    parser.add_argument("--self-test", action="store_true",
                        help="check argument handling and the source switch, then exit")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tabs", help="list the tab ids", parents=[shared])

    p_sessions = sub.add_parser("sessions", help="list sessions, newest first", parents=[shared])
    p_sessions.add_argument("--limit", type=int, default=20)
    p_sessions.add_argument("--json", action="store_true")

    p_dump = sub.add_parser("dump", help="render one tab and print its data", parents=[shared])
    p_dump.add_argument("--tab", required=True)
    p_dump.add_argument("--session", default=None)
    p_dump.add_argument("--scope", default="main", choices=["main", "all"])
    p_dump.add_argument("--cohort", default=None)
    p_dump.add_argument("--compare-with", default=None,
                        help="tab-compare only: the other arm")
    p_dump.add_argument("--compare-kind", default="session", choices=["session", "cohort"])
    p_dump.add_argument("--json", action="store_true")

    p_all = sub.add_parser("all", help="render every tab and report what each produced",
                           parents=[shared])
    p_all.add_argument("--session", default=None)
    p_all.add_argument("--scope", default="main", choices=["main", "all"])
    p_all.add_argument("--cohort", default=None)
    p_all.add_argument("--verbose", action="store_true")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Checked before parsing, because the parser requires a subcommand and `--self-test` is not one.
    if "--self-test" in argv:
        return source.self_test()
    args = build_parser().parse_args(argv)
    # getattr, because the flags use SUPPRESS: absent means "not given", not "given as the default".
    source.configure(getattr(args, "via", source.DASH),
                     getattr(args, "api_url", source.DEFAULT_URL))
    handler = {
        "tabs": commands.cmd_tabs,
        "sessions": commands.cmd_sessions,
        "dump": commands.cmd_dump,
        "all": commands.cmd_all,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
