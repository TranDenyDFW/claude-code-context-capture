"""The defects an independent review found in the first version of the mirror, as gates.

Every test here failed against commit `839aecc`. They are grouped separately from
`tests/test_mirror.py` so the distinction stays visible: that file gates the design, this one gates
the specific ways the design was got wrong.

The one that matters most: a row's working directory arrives from THREE different places and they
do not have to be spelled the same. `sessions.cwd` for a transcript, the raw `~/.claude.json` key
for a config entry, and the string inside the record for a desktop row. The destination lookup was
`mapping.get(row["cwd"], row["cwd"])`, exact string equality, so a project whose config key used
forward slashes had the EXPORTER's path written into the importing user's config, kept the chat
pointed at a directory that does not exist on that machine, and still passed the mirror check.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import appstate, projects  # noqa: E402

SOURCE = r"P:\FakeSrc\Proj"
OTHER_SPELLING = "P:/FakeSrc/Proj"
DEST = r"D:\Landing\Received"
SID = "11111111-1111-4111-8111-111111111111"


class Box:
    """One machine: a home, a config, and a desktop record directory."""

    def __init__(self, root):
        self.claude = root / ".claude"
        self.config = root / ".claude.json"
        self.sessions = root / "appdata" / "Claude" / "claude-code-sessions"
        (self.claude / "projects").mkdir(parents=True)
        (self.claude / "tasks").mkdir(parents=True)
        (self.sessions / "acct" / "org").mkdir(parents=True)
        (self.sessions.parent / "config.json").write_text(
            json.dumps({"lastKnownAccountUuid": "acct"}), encoding="utf-8")
        (self.sessions.parent / "plan-usage-history.json").write_text(
            json.dumps({"samples": [{"t": 1, "org": "org"}]}), encoding="utf-8")

    def use(self, monkeypatch):
        monkeypatch.setattr(appstate, "CLAUDE_DIR", self.claude)
        monkeypatch.setattr(appstate, "CONFIG_PATH", self.config)
        monkeypatch.setattr(appstate, "sessions_root", lambda: str(self.sessions))
        return self


@pytest.fixture
def source_rows(tmp_path, monkeypatch):
    """A capture from a machine whose config key and desktop record use the OTHER slash spelling.

    Not an invented edge: `appstate.normalised` exists in the module because 4 of the 91 projects
    on this machine carry both spellings at once.
    """
    box = Box(tmp_path / "src").use(monkeypatch)
    slug = box.claude / "projects" / appstate.slug_for(SOURCE)
    slug.mkdir(parents=True)
    (slug / f"{SID}.jsonl").write_bytes(b"a line\n")
    box.config.write_text(json.dumps({
        "oauthAccount": {"source machine": True},
        "projects": {OTHER_SPELLING: {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    (box.sessions / "acct" / "org" / "local_x.json").write_text(json.dumps({
        "cliSessionId": SID, "sessionId": "local_x", "cwd": OTHER_SPELLING,
        "originCwd": OTHER_SPELLING, "isArchived": False, "title": "a chat"}), encoding="utf-8")
    rows, _report = appstate.capture([SOURCE], [SID])
    return rows


@pytest.fixture
def destination(tmp_path, monkeypatch, source_rows):
    """A DIFFERENT machine, with its own config, to restore into."""
    box = Box(tmp_path / "dst").use(monkeypatch)
    box.config.write_text(json.dumps({
        "oauthAccount": {"destination machine": True},
        "projects": {r"D:\Something\Else": {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    return box


class TestTheSpellingOfAWorkingDirectory:
    def test_a_config_key_spelled_differently_still_reaches_the_destination(
            self, destination, source_rows):
        """The headline defect. The exporter's absolute path went into this user's config."""
        appstate.restore(source_rows, {SOURCE: DEST})
        config = json.loads(destination.config.read_text(encoding="utf-8"))
        assert DEST in config["projects"], "the destination never got a config entry"
        assert OTHER_SPELLING not in config["projects"], \
            "the EXPORTER's own path was written into this machine's config"
        assert SOURCE not in config["projects"]

    def test_a_record_spelled_differently_still_points_at_the_destination(
            self, destination, source_rows):
        """An imported chat that opens the exporter's directory is the feature failing."""
        appstate.restore(source_rows, {SOURCE: DEST})
        record = json.loads(
            (destination.sessions / "acct" / "org" / "local_x.json").read_text(encoding="utf-8"))
        assert record["cwd"] == DEST
        assert record["originCwd"] == DEST

    def test_the_unrelated_config_keys_are_untouched(self, destination, source_rows):
        appstate.restore(source_rows, {SOURCE: DEST})
        config = json.loads(destination.config.read_text(encoding="utf-8"))
        assert config["oauthAccount"] == {"destination machine": True}
        assert r"D:\Something\Else" in config["projects"]

    def test_a_directory_under_a_mapped_one_moves_with_it(self):
        mapping = {r"P:\Proj": r"D:\Landing"}
        assert appstate.destination_cwd(r"P:\Proj\sub", mapping) == r"D:\Landing\sub"
        assert appstate.destination_cwd("P:/Proj/sub", mapping) == r"D:\Landing\sub"

    def test_a_directory_that_matches_nothing_is_left_alone(self):
        assert appstate.destination_cwd(r"Q:\Other", {r"P:\Proj": r"D:\Landing"}) == r"Q:\Other"


class TestTwoSpellingsCollidingOnOneKey:
    """Resolving the destination properly created this case, which did not exist before.

    Both of a project's `~/.claude.json` keys now land on ONE destination key, and their entries
    need not be equal. Last-one-wins would make the answer depend on dict order, and the mirror
    check would then report the loser as a difference no import could ever clear.
    """

    def test_the_spelling_the_store_uses_wins(self):
        rows = [{"kind": appstate.CONFIG, "cwd": OTHER_SPELLING, "relpath": OTHER_SPELLING,
                 "blob": b"{}", "sha256": "a", "rebased_sha256": "a", "mtime": 1.0},
                {"kind": appstate.CONFIG, "cwd": SOURCE, "relpath": SOURCE,
                 "blob": b"{}", "sha256": "b", "rebased_sha256": "b", "mtime": 1.0}]
        winners, displaced = appstate.config_winners(
            [(row, DEST) for row in rows], {SOURCE: DEST})
        assert winners[DEST]["cwd"] == SOURCE, \
            "the winner should be the spelling every other layer already agrees on"
        assert [d["displaced"] for d in displaced] == [OTHER_SPELLING]

    def test_the_choice_does_not_depend_on_the_order_the_rows_arrive_in(self):
        rows = [{"kind": appstate.CONFIG, "cwd": spelling, "relpath": spelling, "blob": b"{}",
                 "sha256": spelling, "rebased_sha256": spelling, "mtime": 1.0}
                for spelling in (OTHER_SPELLING, SOURCE)]
        forward, _ = appstate.config_winners([(r, DEST) for r in rows], {SOURCE: DEST})
        backward, _ = appstate.config_winners([(r, DEST) for r in reversed(rows)], {SOURCE: DEST})
        assert forward[DEST]["cwd"] == backward[DEST]["cwd"]

    def test_the_displaced_spelling_is_reported_and_is_not_a_difference(
            self, destination, source_rows):
        """It has to be BOTH: named, so nothing is lost silently, and not counted as a defect,
        so the mirror check can still reach ok."""
        extra = dict(source_rows[0])
        for row in source_rows:
            if row["kind"] == appstate.CONFIG:
                extra = dict(row)
                break
        extra["cwd"] = SOURCE
        extra["relpath"] = SOURCE
        # `canonical_json`, the way `_capture_config` builds it. A config row's hash is taken over
        # the canonical form, because the check recomputes it from the parsed entry rather than
        # from whatever whitespace the file happened to carry.
        extra["blob"] = appstate.canonical_json(
            {"hasTrustDialogAccepted": True, "allowedTools": []})
        extra["sha256"] = appstate.sha256_bytes(extra["blob"])
        extra["rebased_sha256"] = extra["sha256"]
        rows = list(source_rows) + [extra]
        report = appstate.restore(rows, {SOURCE: DEST})
        assert report["config_spellings_merged"], "the displaced spelling was not reported"
        result = appstate.compare(rows, {SOURCE: DEST})
        assert result["ok"], result["differs"]


class TestTheCheckSeesWhatTheImportRewrites:
    def test_a_record_pointed_at_nowhere_is_caught(self, destination, source_rows):
        """The blind spot: the hash neutralises `cwd` and `originCwd` so a rebased record can be
        compared at all, which left the only two fields an import REWRITES outside every check.
        A record pointed at a directory that does not exist passed."""
        appstate.restore(source_rows, {SOURCE: DEST})
        assert appstate.compare(source_rows, {SOURCE: DEST})["ok"]
        landed = destination.sessions / "acct" / "org" / "local_x.json"
        record = json.loads(landed.read_text(encoding="utf-8"))
        record["cwd"] = r"Q:\this\does\not\exist"
        record["originCwd"] = r"Q:\this\does\not\exist"
        landed.write_text(json.dumps(record), encoding="utf-8")
        result = appstate.compare(source_rows, {SOURCE: DEST})
        assert not result["ok"]
        assert any(d.get("kind") == appstate.DESKTOP
                   and "not the destination" in (d.get("why") or "")
                   for d in result["differs"])

    def test_the_rest_of_the_record_is_still_hashed(self, destination, source_rows):
        """Closing the blind spot must not open one: everything else stays under the hash."""
        appstate.restore(source_rows, {SOURCE: DEST})
        landed = destination.sessions / "acct" / "org" / "local_x.json"
        record = json.loads(landed.read_text(encoding="utf-8"))
        record["title"] = "renamed by someone"
        landed.write_text(json.dumps(record), encoding="utf-8")
        assert not appstate.compare(source_rows, {SOURCE: DEST})["ok"]


class TestTheDestinationIsChecked:
    """Everything an import writes is derived from this one string, so a bad one does not fail
    loudly: it files the project under a name nothing will look for."""

    @pytest.mark.parametrize("value,why", [
        ("", "empty"),
        ("..\\x", "a parent reference, which slugs to ---x"),
        ("relative\\path", "relative, and the server's directory is not the user's"),
        ("P:\\Books\\archived", "a page LABEL that the archive flag produced, not a directory"),
    ])
    def test_a_destination_that_is_not_a_working_directory_is_refused(self, value, why):
        with pytest.raises(ValueError):
            projects.check_destination(value)

    def test_an_existing_file_is_refused(self, tmp_path):
        target = tmp_path / "notadir.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="existing FILE"):
            projects.check_destination(str(target))

    def test_an_absolute_directory_is_accepted_and_trimmed(self):
        assert projects.check_destination("D:\\Work\\Alpha\\") == "D:\\Work\\Alpha"

    def test_a_unc_path_is_accepted(self):
        unc = chr(92) * 2 + "server" + chr(92) + "share" + chr(92) + "proj"
        assert projects.check_destination(unc) == unc

    def test_the_import_refuses_before_writing_anything(self, tmp_path, destination, source_rows):
        """The check has to run at the boundary, not somewhere inside."""
        manifest = {"cwds": [SOURCE], "primary_cwd": SOURCE}
        with pytest.raises(ValueError):
            projects.destination_mapping(manifest, "relative\\path")


class TestTheConfigIsNeverClobbered:
    """The worst defect in this feature, and it was found by running it on a real second machine
    rather than by any test or any review.

    `_merge_config` read `~/.claude.json` inside `except (OSError, ValueError): config = {}` and
    then wrote its result back, so a file it could not PARSE was replaced by one holding just the
    project being imported. Measured on the test laptop: 26 project entries became 1. All but two
    came back only because Claude Code keeps its own `.claude.json.backup`.

    The trigger was ordinary. Windows PowerShell's `Set-Content -Encoding UTF8` writes a byte order
    mark, `json.loads` raises ValueError on one, and the fallback did the rest.
    """

    def test_a_config_with_a_byte_order_mark_is_read_not_replaced(self, destination, source_rows):
        raw = destination.config.read_text(encoding="utf-8")
        destination.config.write_bytes(b"\xef\xbb\xbf" + raw.encode("utf-8"))
        appstate.restore(source_rows, {SOURCE: DEST})
        config = json.loads(destination.config.read_text(encoding="utf-8-sig"))
        assert r"D:\Something\Else" in config["projects"], \
            "a byte order mark cost this machine every other project's settings"
        assert config["oauthAccount"] == {"destination machine": True}
        assert DEST in config["projects"]

    def test_a_config_that_cannot_be_parsed_stops_the_import(self, destination, source_rows):
        """Refusing is the only safe answer: the alternative is what destroyed the laptop's."""
        destination.config.write_text("{ this is not json", encoding="utf-8")
        with pytest.raises(ValueError, match="Refusing to touch it"):
            appstate.restore(source_rows, {SOURCE: DEST})
        assert destination.config.read_text(encoding="utf-8") == "{ this is not json", \
            "the unparseable file was overwritten anyway"

    def test_a_copy_is_kept_before_the_config_is_replaced(self, destination, source_rows):
        before = destination.config.read_text(encoding="utf-8")
        appstate.restore(source_rows, {SOURCE: DEST})
        kept = destination.config.with_suffix(destination.config.suffix + ".c4x-before")
        assert kept.exists(), "no copy was kept, so a correct write is still not undoable"
        assert kept.read_text(encoding="utf-8") == before

    def test_a_machine_with_no_config_at_all_is_not_an_error(self, destination, source_rows):
        """A missing file is a fresh machine, which is different from an unreadable one."""
        destination.config.unlink()
        appstate.restore(source_rows, {SOURCE: DEST})
        config = json.loads(destination.config.read_text(encoding="utf-8-sig"))
        assert DEST in config["projects"]

