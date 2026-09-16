"""The Claude desktop app as a process: found without a shell, restarted without touching the
server. Every side effect of `c4x/desktop.py` is a parameter, and these drive them."""
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from c4x import desktop  # noqa: E402

MANIFEST = ('<?xml version="1.0"?><Package><Applications><Application Id="Claude" '
            'Executable="app\\Claude.exe" EntryPoint="Windows.FullTrustApplication"/>'
            '</Applications></Package>')


def appx(lines):
    def run(args, **kw):
        assert args[:3] == ["powershell", "-NoProfile", "-Command"] and "Get-AppxPackage" in args[3]
        assert kw.get("capture_output") and kw.get("text")
        return SimpleNamespace(stdout="\n".join(lines) + "\n", returncode=0)
    return run


class TestFindingTheApp:
    def test_the_store_build_is_started_through_the_shell_with_the_manifest_s_id(self):
        lines = ["Claude_pzs8sxrjxfjjc", r"C:\Program Files\WindowsApps\Claude_1"]
        found = desktop.find_app(run=appx(lines), read=lambda path: MANIFEST,
                                 exists=lambda p: False, system="Windows")
        assert found == {"kind": "store", "family": "Claude_pzs8sxrjxfjjc", "app_id": "Claude",
                         "launch": ["explorer.exe",
                                    r"shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude"]}

    def test_the_manifest_is_read_from_the_package_s_own_location(self):
        seen = []

        def read(path):
            seen.append(Path(path))
            return MANIFEST
        desktop.find_app(run=appx(["Claude_x", r"C:\WA\Claude_1"]), read=read,
                         exists=lambda p: False, system="Windows")
        assert seen == [Path(r"C:\WA\Claude_1") / "AppxManifest.xml"]

    def test_a_package_whose_manifest_names_no_application_is_not_launched_blind(self):
        found = desktop.find_app(run=appx(["Claude_x", r"C:\WA\Claude_1"]),
                                 read=lambda path: "<Package/>", exists=lambda p: False,
                                 system="Windows")
        assert found is None, "a guessed id would launch nothing after the kill"

    def test_the_installer_s_exe_is_started_by_its_path(self):
        exe = r"C:\Users\me\AppData\Local\Programs\Claude\Claude.exe"
        local = r"C:\Users\me\AppData\Local"
        found = desktop.find_app(run=appx(["none"]), exists=lambda p: p == exe,
                                 expand=lambda raw: raw.replace("%LOCALAPPDATA%", local),
                                 system="Windows")
        assert found == {"kind": "exe", "launch": [exe]}

    def test_nothing_found_is_none_and_off_windows_nothing_is_asked(self):
        nothing = desktop.find_app(run=appx(["none"]), exists=lambda p: False, system="Windows")
        assert nothing is None

        def never(*a, **k):
            raise AssertionError("powershell was run off Windows")
        assert desktop.find_app(run=never, system="Linux") is None

    def test_a_missing_powershell_is_no_app_not_a_crash(self):
        def missing(*a, **k):
            raise OSError("no powershell")
        assert desktop.find_app(run=missing, exists=lambda p: False, system="Windows") is None


APP_EXE = r"C:\Program Files\WindowsApps\Claude_2.110.0.0_x64__pzs8sxrjxfjjc\app\Claude.exe"
CLI_EXE = r"C:\Users\me\.local\bin\claude.exe"


class _Proc:
    """A process as the table shows it: the name for every one, the executable's path on ask.
    The app's own path unless told otherwise, since most of these tests are about the app."""
    def __init__(self, pid, name, terminates=True, exe=APP_EXE):
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self._exe = exe
        self.terminates = terminates
        self.terminated = False
        self.killed = False

    def name(self):
        return self.info["name"]

    def exe(self):
        if self._exe is psutil.AccessDenied:
            raise psutil.AccessDenied(self.pid)
        return self._exe

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class TestTheProcesses:
    def test_only_the_app_s_image_counts_and_never_this_process(self):
        table = [_Proc(1, "svchost.exe"), _Proc(2, "CLAUDE.EXE"), _Proc(3, "claude.exe"),
                 _Proc(4, "claude-helper.exe"), _Proc(5, None)]
        found = desktop.app_processes(table, self_pid=3)
        assert [p.pid for p in found] == [2], "the image in any case; helper and self left out"

    def test_a_process_that_vanishes_mid_walk_is_skipped(self):
        class Gone(_Proc):
            def name(self):
                raise psutil.NoSuchProcess(self.pid)
        table = [Gone(7, None), _Proc(8, "claude.exe")]
        assert [p.pid for p in desktop.app_processes(table, self_pid=0)] == [8]

    def test_the_cli_is_never_the_app_though_it_shares_the_name(self):
        """Measured on the author's machine, 2026-09-16: 14 app processes and 2 CLI sessions,
        all sixteen named claude.exe; a restart on the name alone would end every terminal."""
        table = [_Proc(1, "claude.exe", exe=CLI_EXE),
                 _Proc(2, "claude.exe",
                       exe=r"C:\Users\me\AppData\Roaming\Claude\claude-code\2.1.270\claude.exe"),
                 _Proc(3, "claude.exe", exe=psutil.AccessDenied),
                 _Proc(4, "claude.exe", exe=None),
                 _Proc(5, "Claude.exe")]
        assert [p.pid for p in desktop.app_processes(table, self_pid=0)] == [5]

    def test_the_path_is_read_only_for_the_ones_named_claude(self):
        asked = []

        class Counting(_Proc):
            def exe(self):
                asked.append(self.pid)
                return super().exe()
        table = [Counting(1, "svchost.exe"), Counting(2, "node.exe"), Counting(3, "claude.exe")]
        desktop.app_processes(table, self_pid=0)
        assert asked == [3], "one handle per claude.exe, none for the rest of the table"


class TestTheIdentityRule:
    @pytest.mark.parametrize("path", [
        APP_EXE,
        r"D:\WindowsApps\Claude_1.52386.6.0_x64__pzs8sxrjxfjjc\app\Claude.exe",
        "C:/Program Files/WindowsApps/Claude_2.110.0.0_x64__pzs8sxrjxfjjc/app/CLAUDE.EXE",
        r"%LOCALAPPDATA%\Programs\Claude\Claude.exe",
        r"%LOCALAPPDATA%\Programs\claude-desktop\Claude.exe",
    ])
    def test_the_app_s_own_executable(self, path):
        assert desktop.is_app_exe(path, expand=lambda raw: raw) is True

    @pytest.mark.parametrize("path", [
        CLI_EXE,
        r"C:\Users\me\AppData\Roaming\Claude\claude-code\2.1.270\claude.exe",
        r"C:\Users\me\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude"
        r"\claude-code\2.1.270\claude.exe",
        r"C:\Program Files\WindowsApps\Claude_2.110.0.0_x64__pzs8sxrjxfjjc\app\resources"
        r"\cowork-svc.exe",
        r"C:\Users\me\AppData\Local\Programs\Claude\Claude.exe.bak",
        "", None, 7,
    ])
    def test_everything_else_is_not(self, path):
        assert desktop.is_app_exe(path, expand=lambda raw: raw) is False

    def test_the_installer_path_is_expanded_before_the_comparison(self):
        local = r"C:\Users\me\AppData\Local"
        grow = lambda raw: raw.replace("%LOCALAPPDATA%", local)  # noqa: E731
        assert desktop.is_app_exe(local + r"\Programs\Claude\Claude.exe", expand=grow) is True
        assert desktop.is_app_exe(local + r"\Programs\Other\Claude.exe", expand=grow) is False


class TestTheRestart:
    def _drive(self, monkeypatch, *, back_after=1, stuck=False, launch_raises=False):
        monkeypatch.setattr(desktop.platform, "system", lambda: "Windows")
        app = {"kind": "store", "launch": ["explorer.exe", r"shell:AppsFolder\F!Claude"]}
        first = [_Proc(10, "claude.exe"), _Proc(11, "claude.exe", terminates=not stuck)]
        state = {"listed": 0, "launched": [], "waited": []}

        def processes():
            state["listed"] += 1
            if state["listed"] == 1:
                return first
            # After the launch: back once `back_after` listings have happened.
            return [_Proc(20, "claude.exe")] if state["listed"] > 1 + back_after else []

        def wait_procs(procs, timeout):
            state["waited"].append((sorted(p.pid for p in procs), timeout))
            return [p for p in procs if p.terminates], [p for p in procs if not p.terminates]

        def launch(argv, **kw):
            state["launched"].append(argv)
            if launch_raises:
                raise OSError("explorer refused")
        clock = {"t": 0.0}

        def now():
            clock["t"] += 1.0
            return clock["t"]
        def task(app, log):
            state.setdefault("tasks", []).append(app["launch"])
            return False, "no task scheduler in a test"
        report = desktop.restart_app(app, launch=launch, processes=processes, wait_procs=wait_procs,
                                     sleep=lambda s: None, now=now, log=lambda m: None,
                                     relaunch_s=5.0, task=task)
        return report, first, state

    def test_every_app_process_is_terminated_then_started_and_seen_back(self, monkeypatch):
        report, first, state = self._drive(monkeypatch)
        assert all(p.terminated for p in first) and not any(p.killed for p in first)
        assert state["launched"] == [["explorer.exe", r"shell:AppsFolder\F!Claude"]]
        assert state["waited"][0] == ([10, 11], desktop.TERMINATE_S)
        assert report == {"restarted": True, "killed": 2, "why": "relaunched",
                          "launch": ["explorer.exe", r"shell:AppsFolder\F!Claude"]}

    def test_a_process_that_ignores_terminate_is_killed(self, monkeypatch):
        report, first, state = self._drive(monkeypatch, stuck=True)
        assert first[1].killed and not first[0].killed
        assert state["waited"][1] == ([11], desktop.KILL_S)
        assert report["restarted"] is True

    def test_an_app_that_does_not_come_back_is_said_so_not_claimed(self, monkeypatch):
        report, _first, state = self._drive(monkeypatch, back_after=99)
        assert report["restarted"] is False and "did not come back" in report["why"]
        assert state["launched"], "the launch was still attempted"

    def test_a_launch_that_fails_is_reported_after_the_kill(self, monkeypatch):
        report, first, _state = self._drive(monkeypatch, launch_raises=True)
        assert all(p.terminated for p in first)
        assert report["restarted"] is False and "the launch failed" in report["why"]

    def test_refused_before_any_process_is_touched(self, monkeypatch):
        def never():
            raise AssertionError("the process table was read")
        monkeypatch.setattr(desktop.platform, "system", lambda: "Linux")
        off = desktop.restart_app({"launch": ["x"]}, processes=never)
        assert off["restarted"] is False and "Windows" in off["why"]
        monkeypatch.setattr(desktop.platform, "system", lambda: "Windows")
        monkeypatch.setattr(desktop, "find_app", lambda: None)
        none = desktop.restart_app(processes=never)
        assert none["restarted"] is False and "no Claude desktop app" in none["why"]
        assert none["killed"] == 0 and none["launch"] is None


def test_the_launch_goes_through_proc_run_with_no_window():
    """The default launcher is `c4x.proc.run`, the package's one door for children."""
    from c4x import proc
    assert desktop.restart_app.__kwdefaults__["launch"] is None
    source = (ROOT / "c4x" / "desktop.py").read_text(encoding="utf-8")
    assert "start = proc.run if launch is None else launch" in source
    assert proc.NO_WINDOW == (0x08000000 if sys.platform == "win32" else 0)


@pytest.mark.parametrize("line", ["Claude_pzs8sxrjxfjjc", "Claude_pzs8sxrjxfjjc!Claude"])
def test_the_laptop_s_measured_identity_is_what_the_query_yields(line):
    """Measured on the test laptop on 2026-09-14: PackageFamilyName `Claude_pzs8sxrjxfjjc`,
    Application Id `Claude`. The AUMID the shell needs is the two joined by `!`."""
    found = desktop.find_app(run=appx(["Claude_pzs8sxrjxfjjc", r"C:\WA\Claude"]),
                             read=lambda p: MANIFEST, exists=lambda p: False, system="Windows")
    assert line in found["launch"][1]
