"""The watchdog's two decisions: what is a Claude process, and when absence becomes a stop.

Process objects are fakes with the three members `claude_alive` reads: `pid`, `info`/`name()`,
and `cmdline()`. `psutil`'s own exceptions are raised by them so the guard under test is the real
one.
"""
import threading

import psutil

from c4x import watchdog


class Proc:
    def __init__(self, name, cmdline=None, pid=100, cmdline_raises=None):
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self._name = name
        self._cmdline = cmdline or []
        self._raises = cmdline_raises

    def name(self):
        return self._name

    def cmdline(self):
        if self._raises:
            raise self._raises
        return list(self._cmdline)


NPM_WINDOWS = ["C:\\Program Files\\nodejs\\node.exe",
               "C:\\Users\\x\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js"]
NPM_LINUX = ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]


def test_the_cli_the_store_app_and_the_npm_layout_are_all_claude():
    assert watchdog.claude_alive([Proc("claude")], self_pid=1)
    assert watchdog.claude_alive([Proc("claude.exe")], self_pid=1)
    assert watchdog.claude_alive([Proc("Claude.exe")], self_pid=1)
    assert watchdog.claude_alive([Proc("node.exe", NPM_WINDOWS)], self_pid=1)
    assert watchdog.claude_alive([Proc("node", NPM_LINUX)], self_pid=1)


def test_other_processes_and_an_empty_table_are_not():
    assert not watchdog.claude_alive([], self_pid=1)
    assert not watchdog.claude_alive([Proc("notclaude"), Proc("explorer.exe"),
                                      Proc("node.exe", ["node", "server.js"])], self_pid=1)


def test_this_process_never_counts_as_claude():
    assert not watchdog.claude_alive([Proc("claude", pid=42)], self_pid=42)
    assert watchdog.claude_alive([Proc("claude", pid=42), Proc("claude", pid=43)], self_pid=42)


def test_a_process_that_refuses_to_be_read_is_skipped_and_the_walk_continues():
    denied = Proc("node.exe", NPM_WINDOWS, pid=7, cmdline_raises=psutil.AccessDenied(7))
    assert not watchdog.claude_alive([denied], self_pid=1)
    assert watchdog.claude_alive([denied, Proc("claude", pid=8)], self_pid=1)
    gone = Proc("node.exe", NPM_WINDOWS, pid=9, cmdline_raises=psutil.NoSuchProcess(9))
    assert watchdog.claude_alive([gone, Proc("Claude.exe", pid=10)], self_pid=1)


def test_the_command_line_is_read_for_node_only():
    """A non-node process with the npm marker in its command line is not Claude: the marker is
    evidence only where the interpreter is node."""
    assert not watchdog.claude_alive([Proc("python.exe", NPM_WINDOWS)], self_pid=1)


def scripted(*answers):
    it = iter(answers)
    return lambda: next(it)


def test_a_stop_comes_once_when_misses_add_up_to_grace_and_not_before():
    stops = []
    dog = watchdog.Watchdog(stop=stops.append,
                            is_alive=scripted(True, True, False, False, False, False, False),
                            interval=15, grace=60)
    assert [dog.tick() for _ in range(3)] == ["alive", "alive", "missing"]
    assert stops == [], "one miss of 15 s is not 60 s"
    assert dog.tick() == "missing" and stops == []
    assert dog.tick() == "missing" and stops == [], "45 s"
    assert dog.tick() == "missing"
    assert stops == ["no Claude process for 60 s"]
    assert dog.tick() == "missing" and len(stops) == 1, "stop is called once"


def test_alive_again_resets_the_count():
    stops = []
    dog = watchdog.Watchdog(stop=stops.append,
                            is_alive=scripted(False, False, False, True, False, False, False),
                            interval=15, grace=60)
    for _ in range(7):
        dog.tick()
    assert stops == [], "three misses, one sighting, three misses: never 60 s in a row"
    assert dog.missing_for == 45.0


def test_a_tick_that_raises_is_unknown_and_does_not_count_as_a_miss():
    stops, lines = [], []
    answers = iter([False, RuntimeError("table"), False, False, False])

    def flaky():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    dog = watchdog.Watchdog(stop=stops.append, is_alive=flaky, interval=15, grace=60,
                            log=lines.append)
    assert dog.tick() == "missing"
    assert dog.tick() == "unknown"
    assert dog.missing_for == 15.0
    assert len(lines) == 1 and "process table" in lines[0]
    dog.tick()
    dog.tick()
    assert stops == [], "15 + 15 + 15 = 45, the unknown tick added nothing"
    dog.tick()
    assert len(stops) == 1


def test_the_thread_stops_the_server_after_grace_with_no_claude():
    stopped = threading.Event()
    dog = watchdog.Watchdog(stop=lambda why: stopped.set(), is_alive=lambda: False,
                            interval=0.001, grace=0.004)
    thread = dog.start()
    assert stopped.wait(2.0), "the daemon thread never called stop"
    thread.join(2.0)
    assert dog.stopped and not thread.is_alive()


def test_the_real_walk_runs_against_this_machine():
    """No assertion on the answer: this suite may or may not run under Claude. The walk itself must
    complete without raising, with psutil's real process table."""
    assert watchdog.claude_alive() in (True, False)
