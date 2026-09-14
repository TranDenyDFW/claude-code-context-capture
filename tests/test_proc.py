"""The server's children open no console, and every spawn in the package goes through one door.

The window came from `pythonw`: a console program started from a console-less parent is given a
console of its own, and `tasklist` ran on every page load. The flag is the fix; the sweep is what
keeps a future call site from bringing the window back.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import proc  # noqa: E402

CREATE_NO_WINDOW = 0x08000000
SPAWN = re.compile(r"\bsubprocess\.(run|call|check_call|check_output|Popen)\s*\(")


def spawn_sites(root: Path) -> list:
    """Every direct subprocess call under `root`, as (posix path relative to root, line)."""
    found = []
    for path in sorted(root.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SPAWN.search(line):
                found.append((path.relative_to(root).as_posix(), n))
    return found


class TestTheFlag:
    def _capture(self, monkeypatch):
        seen: dict = {}

        def fake(args, **kw):
            seen["args"], seen["kw"] = args, kw
            return "ran"
        monkeypatch.setattr(subprocess, "run", fake)
        return seen

    def test_on_windows_the_child_gets_no_window(self, monkeypatch):
        seen = self._capture(monkeypatch)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(proc, "NO_WINDOW", CREATE_NO_WINDOW)
        assert proc.run(["x"], capture_output=True, text=True) == "ran"
        assert seen["args"] == ["x"]
        assert seen["kw"]["capture_output"] is True and seen["kw"]["text"] is True, "passed through"
        assert seen["kw"]["creationflags"] & CREATE_NO_WINDOW, seen["kw"]

    def test_a_flag_the_caller_set_is_kept(self, monkeypatch):
        seen = self._capture(monkeypatch)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(proc, "NO_WINDOW", CREATE_NO_WINDOW)
        proc.run(["x"], creationflags=0x200)
        assert seen["kw"]["creationflags"] == 0x200 | CREATE_NO_WINDOW

    def test_elsewhere_nothing_is_added(self, monkeypatch):
        seen = self._capture(monkeypatch)
        monkeypatch.setattr(sys, "platform", "linux")
        proc.run(["x"], timeout=5)
        assert seen["kw"] == {"timeout": 5}

    def test_the_real_flag_matches_the_platform(self):
        if sys.platform == "win32":
            assert proc.NO_WINDOW == subprocess.CREATE_NO_WINDOW == CREATE_NO_WINDOW
        else:
            assert proc.NO_WINDOW == 0

    def test_a_patched_subprocess_run_is_what_gets_called(self, monkeypatch):
        """`tests/test_delete_layers.py` patches `subprocess.run` to prove nothing shells out to
        the one command the delete design avoids; a reference bound at import would dodge it."""
        calls: list = []
        monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append(args))
        proc.run(["y"])
        assert calls == [["y"]]


class TestOneDoor:
    def test_no_module_spawns_on_its_own(self):
        sites = [s for s in spawn_sites(ROOT / "c4x") if s[0] != "proc.py"]
        assert sites == [], sites

    def test_the_sweep_sees_a_spawn(self, tmp_path):
        """A sweep that cannot fail proves nothing: a known-bad module must be reported."""
        (tmp_path / "bad.py").write_text("import subprocess\n\nsubprocess.run(['x'])\n",
                                         encoding="utf-8")
        (tmp_path / "worse.py").write_text("import subprocess as sp\nsubprocess.Popen (['x'])\n",
                                           encoding="utf-8")
        (tmp_path / "good.py").write_text("from c4x import proc\nproc.run(['x'])\n",
                                          encoding="utf-8")
        assert spawn_sites(tmp_path) == [("bad.py", 3), ("worse.py", 2)]
