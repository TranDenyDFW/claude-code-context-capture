"""The server starting itself again: the command line it builds, the child it spares, and the
one detached spawn in `c4x/proc.py`."""
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from c4x import proc, server  # noqa: E402


class TestTheRelaunchCommand:
    def test_the_same_interpreter_and_flags_plus_after_our_pid(self):
        argv = server.relaunch_argv(["--db", "D:/s.db", "--port", "8061", "--watchdog"],
                                    executable=r"C:\py\pythonw.exe", frozen=False, pid=4242)
        assert argv == [r"C:\py\pythonw.exe", "-m", "c4x.api", "--db", "D:/s.db", "--port", "8061",
                        "--watchdog", "--after", "4242"]

    def test_a_stale_after_from_an_earlier_restart_is_dropped(self):
        argv = server.relaunch_argv(["--after", "7", "--port", "8061", "--after", "8"],
                                    executable="py", frozen=False, pid=9)
        assert argv == ["py", "-m", "c4x.api", "--port", "8061", "--after", "9"]

    def test_frozen_the_exe_is_the_module(self):
        argv = server.relaunch_argv(["--watchdog"], executable=r"X:\c4x.exe", frozen=True, pid=1)
        assert argv == [r"X:\c4x.exe", "--watchdog", "--after", "1"]

    def test_the_defaults_are_this_process(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["c4x/api/__main__.py", "--port", "8059"])
        argv = server.relaunch_argv(frozen=False)
        assert argv[0] == sys.executable and argv[-2:] == ["--after", str(os.getpid())]
        assert "--port" in argv and "8059" in argv


class TestRestartServer:
    def test_the_child_is_started_detached_into_the_log_and_spared_from_the_shutdown(
            self, monkeypatch, tmp_path):
        spawned = []
        stopped = []

        def detach(argv, log, cwd=None):
            spawned.append((argv, Path(log), cwd))
            return SimpleNamespace(pid=777)
        monkeypatch.setattr(server, "hardened_shutdown",
                            lambda reason, spare=(): stopped.append((reason, list(spare))))
        monkeypatch.setattr(sys, "argv", ["main", "--port", "8061"])
        answer = server.restart_server("Restart C4X button", log_path=tmp_path / "dashboard.log",
                                       detach=detach)
        assert answer["restarting"] is True and answer["pid"] == 777
        assert spawned[0][0] == answer["argv"] and spawned[0][1] == tmp_path / "dashboard.log"
        assert spawned[0][2] is not None, "the child runs from the install root"
        assert stopped == [("restart (Restart C4X button)", [777])]

    def test_the_log_defaults_to_the_dashboard_log_beside_the_store(self, monkeypatch, tmp_path):
        from c4x import store
        monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "context.db")
        monkeypatch.setattr(server, "hardened_shutdown", lambda reason, spare=(): None)
        seen = []

        def detach(argv, log, cwd=None):
            seen.append(Path(log))
            return SimpleNamespace(pid=1)
        server.restart_server("x", detach=detach)
        assert seen == [tmp_path / "data" / "raw" / "dashboard.log"]


class TestDetach:
    def test_a_child_outlives_the_call_and_its_output_lands_in_the_log(self, tmp_path):
        log = tmp_path / "raw" / "child.log"
        child = proc.detach([sys.executable, "-c", "print('spoken by the child')"], log,
                            cwd=tmp_path)
        assert child.pid > 0
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
        assert child.poll() == 0
        assert "spoken by the child" in log.read_text(encoding="utf-8")

    def test_it_appends_rather_than_truncating(self, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("first server's token line\n", encoding="utf-8")
        child = proc.detach([sys.executable, "-c", "print('second')"], log)
        child.wait(20)
        text = log.read_text(encoding="utf-8")
        assert text.startswith("first server's token line") and "second" in text

    def test_on_windows_the_child_is_its_own_detached_group_without_a_window(self):
        if sys.platform != "win32":
            assert proc.DETACHED == 0
            return
        assert proc.DETACHED == (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        assert proc.NO_WINDOW == subprocess.CREATE_NO_WINDOW

    def test_the_spawn_lives_in_proc_and_nowhere_else(self):
        from tests.test_proc import spawn_sites
        assert [s for s in spawn_sites(ROOT / "c4x") if s[0] != "proc.py"] == []
