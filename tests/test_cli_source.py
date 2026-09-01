"""The CLI's backend switch: `--via dash` and `--via api` must reach the same numbers.

Two things are checked here and they fail for different reasons.

The first is ARGUMENT HANDLING, which needs no store and no server. It looks like a formality and is
not: argparse only accepts a top-level option BEFORE the subcommand, so `c4x.cli all --via api` (the
order a person actually types) was rejected outright, and the shared-parent fix for that has its own
trap where the subparser's defaults overwrite an option already parsed, silently sending the command
to the wrong backend while printing nothing. Both are checked below because both were real.

The second is AGREEMENT: the same sweep run through both backends reports the same tables, figures
and rows. That is `tools/parity.py`'s job in depth; what is checked here is that the CLI's own path
to each backend works at all, which parity does not cover because it calls the backends directly.
"""
import pytest

from c4x.cli import source
from c4x.cli.__main__ import build_parser


@pytest.fixture(autouse=True)
def restore_default_backend():
    """Every test leaves the switch where it found it.

    `source` holds module state, and a test that flips it to `api` and returns would send every
    later test in the process at an HTTP server that is not running.
    """
    yield
    source.configure(source.DASH)


def parse(argv):
    args = build_parser().parse_args(argv)
    return (getattr(args, "via", source.DASH), getattr(args, "api_url", source.DEFAULT_URL))


def test_the_backend_flag_is_accepted_after_the_subcommand():
    """`c4x.cli all --via api` is the order people type, and it used to be an error."""
    assert parse(["all", "--via", "api"])[0] == source.API


def test_the_backend_flag_is_accepted_before_the_subcommand():
    assert parse(["--via", "api", "all"])[0] == source.API


def test_a_flag_given_before_the_subcommand_is_not_reset_by_the_subcommand():
    """The trap in the fix for the test above.

    A subparser writes its own defaults into the namespace AFTER the main parser has filled it, so
    with an ordinary `default=` the value parsed before the subcommand is silently replaced. The
    command would then run against the dashboard while the reader believes they asked for the API,
    and nothing anywhere would say so.
    """
    via, _ = parse(["--via", "api", "dump", "--tab", "tab-summary"])
    assert via == source.API, "the subcommand's default overwrote an explicitly given flag"


def test_the_url_survives_the_same_trip():
    assert parse(["--via", "api", "--api-url", "http://x:1", "all"])[1] == "http://x:1"
    assert parse(["all", "--via", "api", "--api-url", "http://x:1"])[1] == "http://x:1"


def test_the_default_backend_is_the_dashboard():
    """The API is opt-in for as long as Dash is the app. A default that needed a running server
    would break every existing use of this CLI."""
    assert parse(["all"])[0] == source.DASH


def test_an_unknown_backend_is_refused():
    with pytest.raises(SystemExit):
        source.configure("carrier-pigeon")


def test_the_api_default_is_not_the_dashboards_port():
    """Windows lets a second process bind a port already in use rather than refusing it, so a
    shared port means two servers answering and no way to tell which replied."""
    assert "8056" not in source.DEFAULT_URL


def test_the_source_says_which_backend_it_read():
    source.configure(source.DASH)
    assert "dashboard" in source.describe_source()
    source.configure(source.API, "http://127.0.0.1:9")
    assert "127.0.0.1:9" in source.describe_source()


def test_an_unreachable_api_says_what_to_start(has_store):
    """A refused connection means the server is not running, and `ConnectError` on its own sends
    the reader hunting for a bug instead of a terminal."""
    source.configure(source.API, "http://127.0.0.1:1")
    with pytest.raises(SystemExit) as raised:
        source.tab_ids()
    assert "python -m c4x.api" in str(raised.value)


def test_the_dash_backend_still_renders_a_pane(has_store, session_id):
    source.configure(source.DASH)
    payload = source.render_tab("tab-compactions", session_id)
    assert "tables" in payload and "figures" in payload


def test_an_unknown_tab_names_the_real_ones(has_store):
    source.configure(source.DASH)
    with pytest.raises(SystemExit) as raised:
        source.render_tab("tab-nope")
    assert "tab-summary" in str(raised.value)
