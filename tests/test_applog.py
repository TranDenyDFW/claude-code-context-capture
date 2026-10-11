r"""The app's log is the only place a REFUSED record write appears.

`accounts.verify()` checks that the sharing junctions still land where they were asked to, which is
the read side, and the read side is fine: the chat list loads through a junction. Build 2.26454.0.0
refuses to WRITE through one, with `PlantDetectedError` out of `ensureStorageDir`, and says so in
its log and nowhere else. Every case here is built from the real line, including the part that made
a first attempt count twice: the app logs the error object inline and the refused path appears a
second time inside the escaped stack.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import applog  # noqa: E402

ROOT_DIR = r"C:\Users\Shake\AppData\Roaming\Claude\claude-code-sessions"
PAIR = ROOT_DIR + r"\ba7ccf25-6e12-4287-b331-2d7eff00cdf8\2108d9a2-5d2f-4a2a-9f6b-7f0f9b2d1a55"
REAL = ROOT_DIR + r"\54e8e2c2-6d3a-4c51-8ab7-5b84c31e6e4a\5f6eb959-46b3-4fa4-8ce4-79017924010c"


def refusal(stamp="2026-10-10 12:47:50", session="local_bf4d3e5e-0000-4000-8000-000000000000",
            path=PAIR):
    """One real refusal line, stack and all, as the app writes it."""
    escaped = path.replace("\\", "\\\\")
    return (f"{stamp} [error] Failed to save session {session}: Refusing non-directory at private "
            f"dir path (symlink/file plant): {path} {{ name: 'PlantDetectedError', code: "
            f"undefined, stack: 'PlantDetectedError: Refusing non-directory at private dir path "
            f"(symlink/file plant): {escaped}\\n at Bfe (...)\\n at async e.ensureStorageDir "
            f"(...)\\n at async e.writeSessionToDisk (...)' }}")


class TestOneLineIsOneRefusal:
    def test_the_real_line_counts_once(self):
        out = applog.refusals([refusal()])
        assert out["count"] == 1, "the path inside the escaped stack must not count a second time"

    def test_the_pair_is_the_two_components_under_the_sessions_directory(self):
        out = applog.refusals([refusal()])
        assert list(out["pairs"]) == ["ba7ccf25-6e12-4287-b331-2d7eff00cdf8"
                                      "\\2108d9a2-5d2f-4a2a-9f6b-7f0f9b2d1a55"]
        assert applog.pair_of(REAL).startswith("54e8e2c2")

    def test_a_path_that_names_no_pair_is_unknown(self):
        assert applog.pair_of(r"C:\Users\Shake\AppData\Roaming\Claude") == applog.UNKNOWN
        assert applog.pair_of("") == applog.UNKNOWN

    def test_the_day_and_the_edges_come_from_the_stamp(self):
        out = applog.refusals([refusal(stamp="2026-09-14 23:11:02"),
                               refusal(stamp="2026-10-10 12:47:50")])
        assert out["days"] == {"2026-09-14": 1, "2026-10-10": 1}
        assert (out["first"], out["last"]) == ("2026-09-14 23:11:02", "2026-10-10 12:47:50")

    def test_a_line_with_no_stamp_is_still_counted(self):
        out = applog.refusals(["Failed to save session local_a: Refusing non-directory at private "
                               f"dir path (symlink/file plant): {PAIR}"])
        assert out["count"] == 1
        assert out["days"] == {applog.UNKNOWN: 1}
        assert out["first"] is None

    def test_sessions_are_named_once_each_with_their_counts(self):
        lines = [refusal(session="local_aaaa1111-0000-4000-8000-000000000000"),
                 refusal(session="local_aaaa1111-0000-4000-8000-000000000000"),
                 refusal(session="local_bbbb2222-0000-4000-8000-000000000000")]
        out = applog.refusals(lines)
        assert out["sessions"] == ["local_aaaa1111-0000-4000-8000-000000000000",
                                   "local_bbbb2222-0000-4000-8000-000000000000"]
        assert out["session_counts"]["local_aaaa1111-0000-4000-8000-000000000000"] == 2


class TestWhatMustNotCount:
    """The gate fed what it must ignore. A detector that answers yes to everything is no detector.

    These are the lines that sit beside the real failure in the same log, in their thousands: a
    successful archive, the load that reads the shared directory, and prose that names the error
    class without being one.
    """

    @pytest.mark.parametrize("line", [
        "2026-10-10 12:36:06 [info] Archived session local_aa4b01d4-d188-4555-aa0e-ef2a9167b377",
        "2026-10-10 12:36:06 [info] LocalSessions.archive: sessionId=local_aa4b01d4",
        f"2026-10-10 12:47:49 [info] Loaded 96 persisted sessions from {REAL} (105 archived "
        "deferred, 105 of them unopened; hint used)",
        "2026-10-10 12:00:00 [info] the sharing feature risks a PlantDetectedError one day",
        "2026-10-10 12:00:00 [error] Failed to save session local_x: EPERM: operation not "
        "permitted",
        "",
    ])
    def test_it_is_not_a_refusal(self, line):
        out = applog.refusals([line])
        assert out["count"] == 0
        assert out["pairs"] == {}
        assert out["sessions"] == []

    def test_a_clean_log_reads_as_clean_rather_than_as_unknown(self):
        out = applog.refusals(["2026-10-10 12:00:00 [info] nothing to see"])
        assert applog.sentence(out) == "no refused writes in the app's log"

    def test_the_app_s_own_scheduled_task_file_is_a_refusal_but_not_a_chat(self):
        """Six of this machine's 2,843 are this line, and the EARLIEST refusal is one of them, so
        a count that calls them all record writes is wrong about the number and the date."""
        line = ("2026-09-14 23:31:41 [error] [CCDScheduledTasks] Failed to save scheduled tasks: "
                "Refusing non-directory at private dir path (symlink/file plant): "
                f"{PAIR} {{ name: 'PlantDetectedError' }}")
        out = applog.refusals([line, refusal()])
        assert (out["count"], out["records"], out["other"]) == (2, 1, 1)
        assert len(out["sessions"]) == 1, "the scheduled task refusal names no chat"
        assert "the app's own scheduled task file" in applog.sentence(out)


class TestAcrossTheLogFiles:
    def test_every_file_is_named_with_its_own_count_and_the_totals_add(self, tmp_path):
        a = tmp_path / "main.log"
        b = tmp_path / "main1.log"
        a.write_text("\n".join([refusal(stamp="2026-10-05 01:02:03"), "noise"]), encoding="utf-8")
        b.write_text(refusal(stamp="2026-09-14 10:00:00", path=REAL), encoding="utf-8")
        out = applog.scan([a, b])
        assert out["count"] == 2
        assert sorted(f["count"] for f in out["files"]) == [1, 1]
        assert (out["first"], out["last"]) == ("2026-09-14 10:00:00", "2026-10-05 01:02:03")
        assert len(out["pairs"]) == 2, "one refusal per pair, each kept apart"

    def test_a_file_it_cannot_read_is_reported_and_not_fatal(self, tmp_path):
        good = tmp_path / "main.log"
        good.write_text(refusal(), encoding="utf-8")
        out = applog.scan([good, tmp_path / "gone.log"])
        assert out["count"] == 1
        assert [f for f in out["files"] if "error" in f], "the unreadable file is named"

    def test_the_sentence_says_what_and_where_and_when(self, tmp_path):
        log = tmp_path / "main.log"
        log.write_text("\n".join([refusal(stamp="2026-09-14 10:00:00"),
                                  refusal(stamp="2026-10-10 12:47:50")]), encoding="utf-8")
        said = applog.sentence(applog.scan([log]))
        assert "2 write(s) the app refused through a link" in said
        assert "2 of them chat record(s) for 1 chat(s)" in said
        assert "2026-09-14 10:00:00 to 2026-10-10 12:47:50" in said
        assert "ba7ccf25" in said


class TestWhereItLooks:
    def test_only_directories_that_exist_are_offered(self, tmp_path):
        here, gone = tmp_path / "Logs", tmp_path / "nowhere"
        here.mkdir()
        dirs = applog.log_dirs(candidates=[here, gone], exists=lambda p: Path(p).is_dir())
        assert [str(d) for d in dirs] == [str(here)]

    def test_one_directory_under_two_names_is_read_once(self, tmp_path):
        """THE DEFECT THIS RECORDS, found by an independent review. The Store build virtualises
        `%APPDATA%\\Claude` into its package's `LocalCache`, so both spellings open the same
        directory; the string comparison that deduped the candidates kept both, and `scan()` read
        every log twice and SUMMED. Dormant only because the app stopped logging there before the
        refusals began. Identity, not spelling, decides."""
        here = tmp_path / "Logs"
        here.mkdir()
        twice = [here, tmp_path / "Packages" / "LocalCache" / "Logs"]
        (tmp_path / "Packages" / "LocalCache").mkdir(parents=True)
        (tmp_path / "Packages" / "LocalCache" / "Logs").mkdir()
        same = applog.log_dirs(candidates=twice, identity=lambda p: ("one", "directory"))
        assert len(same) == 1, "two spellings of one directory are one candidate"
        apart = applog.log_dirs(candidates=twice, identity=lambda p: (p, 1))
        assert len(apart) == 2, "two real directories are still two candidates"

    def test_a_directory_the_platform_cannot_identify_is_still_offered(self, tmp_path):
        """Windows answers inode 0 for some objects, and two of those would compare equal."""
        here = tmp_path / "Logs"
        here.mkdir()
        assert applog.log_dirs(candidates=[here], identity=lambda p: None) == [here]

    def test_the_newest_location_is_offered_first(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
        said = [str(p) for p in applog._default_candidates()]
        assert said[0] == str(tmp_path / "local" / "Claude" / "Logs")
        assert len(said) == len({os.path.normcase(s) for s in said}), "no spelling twice"

    def test_the_files_it_reads_are_the_log_files_sorted(self, tmp_path):
        for name in ("main1.log", "main.log", "notes.txt"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        names = [p.name for p in applog.log_files(dirs=[tmp_path])]
        assert names == ["main.log", "main1.log"]
