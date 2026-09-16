"""The confirmed restart: quit, act, relaunch (or act, quit, relaunch), the launch that falls back
to the interactive session's task scheduler, and the window the watchdog waits through. Every
side effect of `c4x/desktop.py` is a parameter, and these drive them in order."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from c4x import desktop  # noqa: E402
from tests.test_desktop import _Proc  # noqa: E402

APP = {"kind": "store", "launch": ["explorer.exe", r"shell:AppsFolder\F!Claude"]}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


class TestTheLaunch:
    def _launch(self, *, shell_back=True, task_starts=True, task_back=True, shell_raises=False):
        """`shell_back`: processes appear after the shell activation; `task_*`: after the task."""
        state = {"listed": 0, "launched": [], "tasks": []}

        def processes():
            state["listed"] += 1
            phase = "task" if state["tasks"] else "shell"
            back = task_back if phase == "task" else shell_back
            return [_Proc(20, "claude.exe")] if back and state["listed"] > 1 else []

        def launch(argv, **kw):
            state["launched"].append(argv)
            if shell_raises:
                raise OSError("explorer refused")

        def task(app, log):
            state["tasks"].append(app["launch"])
            state["listed"] = 0
            return (task_starts, "task" if task_starts else "the task launch failed: denied")
        report = desktop.launch_app(APP, launch=launch, processes=processes, sleep=lambda s: None,
                                    now=Clock(), log=lambda m: None, relaunch_s=5.0, task=task)
        return report, state

    def test_the_shell_activation_first_and_nothing_else_when_it_works(self):
        report, state = self._launch()
        assert report["launched"] is True and report["how"] == "shell"
        assert state["launched"] == [APP["launch"]] and state["tasks"] == []

    def test_the_task_scheduler_when_the_activation_brings_nothing_back(self):
        """Measured on the test laptop, 2026-09-16: the activation from the hook-started server
        returned and no process came; the task in the interactive session brought twelve."""
        report, state = self._launch(shell_back=False)
        assert report["launched"] is True and report["how"] == "task"
        assert state["tasks"] == [APP["launch"]]
        assert report["why"] == "relaunched through the task scheduler"

    def test_the_task_scheduler_when_the_activation_itself_fails(self):
        report, state = self._launch(shell_raises=True)
        assert report["launched"] is True and report["how"] == "task"

    def test_both_failing_is_said_with_both_reasons(self):
        report, _state = self._launch(shell_back=False, task_starts=False)
        assert report["launched"] is False and report["how"] is None
        assert "did not come back within 5 s" in report["why"] and "denied" in report["why"]
        report, _state = self._launch(shell_back=False, task_back=False)
        assert report["launched"] is False and "did not bring it back within 5 s" in report["why"]

    def test_the_task_command_names_the_launch_the_user_and_a_task_it_removes(self):
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return SimpleNamespace(stdout="started\n", stderr="")
        ok, how = desktop.launch_by_task(APP, run=run, user="Administrator", log=lambda m: None)
        assert ok is True and how == "task"
        assert seen[0][:3] == ["powershell", "-NoProfile", "-Command"]
        script = seen[0][3]
        assert ("New-ScheduledTaskAction -Execute 'explorer.exe'"
                " -Argument 'shell:AppsFolder\\F!Claude'") in script
        assert "-UserId 'Administrator' -LogonType Interactive -RunLevel Limited" in script
        assert "Register-ScheduledTask" in script and "Start-ScheduledTask" in script
        assert "Unregister-ScheduledTask" in script and "-Confirm:$false" in script

    def test_the_task_reports_a_shell_that_did_not_say_started(self, monkeypatch):
        def run(argv, **kw):
            return SimpleNamespace(stdout="", stderr="Access is denied")
        ok, why = desktop.launch_by_task(APP, run=run, user="me", log=lambda m: None)
        assert ok is False and "Access is denied" in why
        # No user given and none in the environment: nothing to run the task as.
        monkeypatch.delenv("USERNAME", raising=False)
        assert desktop.launch_by_task(APP, run=run, user="", log=lambda m: None) == (
            False, "no user name to run the task as")

    def test_an_installer_exe_is_the_task_s_action_with_no_argument(self):
        seen = []

        def run(argv, **kw):
            seen.append(argv[3])
            return SimpleNamespace(stdout="started", stderr="")
        desktop.launch_by_task({"kind": "exe", "launch": [r"C:\Apps\Claude's\Claude.exe"]},
                               run=run, user="me", log=lambda m: None)
        assert "-Execute 'C:\\Apps\\Claude''s\\Claude.exe'" in seen[0]
        assert "-Argument" not in seen[0]


class TestWithRestart:
    def _drive(self, monkeypatch, *, quit_first, running=True, quit_ok=True, back=True,
               needed=None, action_raises=False, app=APP):
        monkeypatch.setattr(desktop.platform, "system", lambda: "Windows")
        order = []

        def quit(log):
            order.append("quit")
            return {"quit": quit_ok, "killed": 3 if quit_ok else 1}

        def launch(found, log):
            order.append("launch")
            return {"launched": back, "launch": found["launch"], "how": "shell" if back else None,
                    "why": "relaunched" if back else "the app did not come back within 20 s"}

        def action():
            order.append("act")
            if action_raises:
                raise ValueError("the fold failed")
            return {"done": True, "restart_required": True}
        clock = Clock()
        result, report = desktop.with_restart(action, quit_first=quit_first, needed=needed,
                                              app=app, running=lambda: running, quit=quit,
                                              launch=launch, log=lambda m: None, clock=clock)
        return result, report, order

    def test_quit_act_relaunch_when_the_app_must_be_closed_first(self, monkeypatch):
        result, report, order = self._drive(monkeypatch, quit_first=True)
        assert order == ["quit", "act", "launch"]
        assert result == {"done": True, "restart_required": True}
        assert report == {"was_running": True, "quit": True, "killed": 3, "relaunched": True,
                          "how": "shell", "launch": APP["launch"], "why": "relaunched"}

    def test_act_quit_relaunch_for_a_write_the_app_may_stay_open_for(self, monkeypatch):
        _result, report, order = self._drive(monkeypatch, quit_first=False)
        assert order == ["act", "quit", "launch"] and report["relaunched"] is True

    def test_no_restart_when_the_write_changed_nothing_the_app_would_read(self, monkeypatch):
        _result, report, order = self._drive(monkeypatch, quit_first=False,
                                             needed=lambda r: False)
        assert order == ["act"] and report["quit"] is False and report["relaunched"] is False
        assert "nothing changed" in report["why"]

    def test_the_action_runs_plainly_when_the_app_is_not_running(self, monkeypatch):
        _result, report, order = self._drive(monkeypatch, quit_first=True, running=False)
        assert order == ["act"] and report["was_running"] is False
        assert "was not running" in report["why"]

    def test_off_windows_the_action_runs_plainly_too(self, monkeypatch):
        monkeypatch.setattr(desktop.platform, "system", lambda: "Linux")
        calls = []
        result, report = desktop.with_restart(lambda: calls.append(1) or {"ok": 1},
                                              quit_first=True, running=lambda: True,
                                              quit=lambda log: 1 / 0, launch=lambda f, log: 1 / 0)
        assert result == {"ok": 1} and calls == [1] and "Windows" in report["why"]

    def test_a_failed_relaunch_is_said_not_claimed(self, monkeypatch):
        _result, report, order = self._drive(monkeypatch, quit_first=True, back=False)
        assert order == ["quit", "act", "launch"] and report["relaunched"] is False
        assert report["why"].startswith("Claude is closed: ")
        assert "start it by hand" in report["why"]

    def test_an_app_that_will_not_close_changes_nothing(self, monkeypatch):
        with pytest.raises(RuntimeError, match="could not be closed"):
            self._drive(monkeypatch, quit_first=True, quit_ok=False)

    def test_an_action_that_fails_after_the_quit_brings_the_app_back_then_raises(
            self, monkeypatch):
        with pytest.raises(ValueError, match="the fold failed"):
            self._drive(monkeypatch, quit_first=True, action_raises=True)

    def test_refused_before_any_process_is_touched_when_the_app_cannot_be_restarted(
            self, monkeypatch):
        # `app=None` means "ask find_app", which here finds nothing.
        monkeypatch.setattr(desktop, "find_app", lambda: None)
        with pytest.raises(RuntimeError, match="Nothing has been changed"):
            self._drive(monkeypatch, quit_first=True, app=None)
        # A write the app may stay open for still runs; the report says to restart by hand.
        _result, report, order = self._drive(monkeypatch, quit_first=False, app=None)
        assert order == ["act"] and "restart it yourself" in report["why"]

    def test_one_at_a_time(self, monkeypatch):
        monkeypatch.setattr(desktop.platform, "system", lambda: "Windows")
        desktop._restart_lock.acquire()
        try:
            with pytest.raises(desktop.RestartBusy):
                desktop.with_restart(lambda: {}, quit_first=True, running=lambda: True, app=APP)
        finally:
            desktop._restart_lock.release()

    def test_the_watchdog_is_told_to_wait_through_the_window_and_no_longer(self, monkeypatch):
        monkeypatch.setattr(desktop.platform, "system", lambda: "Windows")
        seen = []

        def action():
            seen.append(desktop.restart_in_progress(now=lambda: 1.0))
            return {"restart_required": True}
        desktop.with_restart(action, quit_first=True, app=APP, running=lambda: True,
                             quit=lambda log: {"quit": True, "killed": 1},
                             launch=lambda f, log: {"launched": True, "launch": [], "how": "shell",
                                                    "why": "relaunched"},
                             log=lambda m: None, clock=lambda: 0.0)
        assert seen == [True], "alive to the watchdog while the action ran"
        assert desktop.restart_in_progress(now=lambda: 1.0) is False, "and not afterwards"
        assert desktop.BUSY_S >= desktop.TERMINATE_S + desktop.KILL_S + 2 * desktop.RELAUNCH_S
