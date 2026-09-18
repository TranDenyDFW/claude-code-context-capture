"""`c4x.exe <verb>`: what each word means, decided from argv alone.

The user's rule for the download: "Make it purely an executable - if someone wants to use CLI
options, they can add options after calling the executable." So every one of these asserts the
same two things: the right tool is chosen, and whatever the person typed after the verb reaches it
unchanged.

No node, no browser, no server here. `plan()` is pure and `run()` takes every door as a parameter,
which is the point of splitting them.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from c4x import verbs

ROOT = Path("C:/Users/me/c4x")
EXE = "C:/Users/me/c4x/c4x.exe"


def plan(argv, frozen=True):
    return verbs.plan(list(argv), frozen=frozen, root=ROOT, exe=EXE)


class TestTheVerbs:
    def test_install_runs_the_installer_with_the_word_and_every_flag(self):
        got = plan(["install", "--no-dashboard", "--adopt", "D:/old"])
        assert got["kind"] == "node"
        assert got["argv"] == [str(ROOT / "tools" / "install.mjs"), "install",
                               "--no-dashboard", "--adopt", "D:/old"]

    @pytest.mark.parametrize("verb", ["status", "uninstall", "reset"])
    def test_the_installers_other_words_are_passed_through_too(self, verb):
        got = plan([verb])
        assert got["argv"] == [str(ROOT / "tools" / "install.mjs"), verb]

    def test_harvest_runs_the_harvester_and_not_the_installer(self):
        got = plan(["harvest", "--stats"])
        # The harvester takes no verb of its own, so the word is consumed and the flags go on.
        assert got["argv"] == [str(ROOT / "tools" / "harvest.mjs"), "--stats"]

    def test_serve_is_the_server_with_the_verb_removed(self):
        got = plan(["serve", "--db", "D:/s.db", "--port", "9000"])
        assert got == {"kind": "serve", "argv": ["--db", "D:/s.db", "--port", "9000"]}

    def test_a_bare_flag_list_is_still_the_server_it_has_always_been(self):
        # WHAT THIS PROTECTS. `tools/dashboard.mjs` launches this exe with exactly this shape and
        # `c4x/server.py` restarts it with its own argv; neither knows a verb exists.
        got = plan(["--db", "D:/s.db", "--port", "8059", "--watchdog"])
        assert got == {"kind": "serve", "argv": ["--db", "D:/s.db", "--port", "8059", "--watchdog"]}

    def test_a_word_it_does_not_know_says_so_and_exits_2(self):
        got = plan(["instal"])
        assert got["kind"] == "say" and got["code"] == 2
        assert "no verb 'instal'" in got["text"]
        assert "c4x install [flags]" in got["text"]

    def test_help_prints_the_usage_and_exits_0(self):
        for word in ["--help", "-h", "help"]:
            got = plan([word])
            assert got["kind"] == "say" and got["code"] == 0 and "c4x serve" in got["text"]

    def test_version_is_a_verb_of_its_own(self):
        assert plan(["--version"])["kind"] == "version"
        assert plan(["-V"])["kind"] == "version"


class TestTheFirstRun:
    def test_no_arguments_from_the_exe_installs_starts_and_opens(self):
        # HOW THE DOWNLOAD IS MET: somebody unzips a folder and double-clicks the program. The
        # order is the whole of it, and it is the order a person would have typed.
        got = plan([])
        assert got["kind"] == "first-run"
        assert [step["kind"] for step in got["steps"]] == ["node", "node", "open"]
        assert got["steps"][0]["argv"] == [str(ROOT / "tools" / "install.mjs"), "install",
                                           "--launcher", EXE]
        assert got["steps"][1]["argv"] == [str(ROOT / "tools" / "dashboard.mjs"), "launch"]

    def test_no_arguments_from_a_checkout_is_the_server(self):
        # `python -m c4x.api` with nothing after it has always been the server, and a developer
        # typing it does not want their settings.json rewritten.
        assert plan([], frozen=False) == {"kind": "serve", "argv": []}


class TestRunning:
    def each(self):
        calls = []
        said = []
        opened = []
        return calls, said, opened, {
            "root": ROOT,
            "node": "C:/Users/me/c4x/node/node.exe",
            "serve": lambda rest: calls.append(("serve", rest)) or 0,
            "call": lambda argv, cwd=None: calls.append((argv, cwd))
                    or SimpleNamespace(returncode=0),
            "open_page": lambda: opened.append(True),
            "write": said.append,
            "version": lambda: "c4x 0.2.0 (abc1234), built today",
        }

    def test_a_node_verb_runs_the_bundled_node_from_the_install(self):
        calls, _said, _opened, doors = self.each()
        assert verbs.run(plan(["status"]), **doors) == 0
        assert calls == [(["C:/Users/me/c4x/node/node.exe",
                           str(ROOT / "tools" / "install.mjs"), "status"], str(ROOT))]

    def test_a_failing_tool_is_this_program_s_exit_code(self):
        calls, _said, _opened, doors = self.each()
        doors["call"] = lambda argv, cwd=None: calls.append(argv) or SimpleNamespace(returncode=3)
        assert verbs.run(plan(["install"]), **doors) == 3

    def test_the_first_run_stops_at_the_step_that_failed_and_opens_nothing(self):
        calls, _said, opened, doors = self.each()
        doors["call"] = (lambda argv, cwd=None: calls.append(argv)
                         or SimpleNamespace(returncode=0 if "install.mjs" in argv[1] else 4))
        assert verbs.run(plan([]), **doors) == 4
        assert len(calls) == 2 and opened == []

    def test_the_first_run_opens_the_page_once_both_steps_worked(self):
        calls, _said, opened, doors = self.each()
        assert verbs.run(plan([]), **doors) == 0
        assert len(calls) == 2 and opened == [True]

    def test_serve_hands_the_flags_back_to_the_server(self):
        calls, _said, _opened, doors = self.each()
        assert verbs.run(plan(["serve", "--port", "9000"]), **doors) == 0
        assert calls == [("serve", ["--port", "9000"])]

    def test_saying_something_writes_it_and_returns_the_code(self):
        _calls, said, _opened, doors = self.each()
        assert verbs.run(plan(["nonsense"]), **doors) == 2
        assert said and "no verb" in said[0]

    def test_a_missing_node_is_a_sentence_and_not_a_traceback(self):
        # A folder somebody unzipped always has its node. An antivirus that quarantined node.exe
        # puts it in this state, and every hook then fails with nothing on screen to explain it.
        _calls, said, _opened, doors = self.each()

        def gone(argv, cwd=None):
            raise FileNotFoundError(2, "The system cannot find the file specified")

        doors["call"] = gone
        assert verbs.run(plan(["status"]), **doors) == 2
        assert said and "node/node.exe" in said[0] and "unzip the download again" in said[0]

    def test_the_first_run_says_the_same_thing_rather_than_raising(self):
        _calls, said, opened, doors = self.each()

        def gone(argv, cwd=None):
            raise FileNotFoundError(2, "no such file")

        doors["call"] = gone
        assert verbs.run(plan([]), **doors) == 2
        assert opened == [] and said

    def test_the_version_comes_from_the_build_stamp(self):
        _calls, said, _opened, doors = self.each()
        assert verbs.run(plan(["--version"]), **doors) == 0
        assert said == ["c4x 0.2.0 (abc1234), built today\n"]
