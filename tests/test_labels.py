"""c4x.labels: a shortened path still tells one project from another.

These need no store. The defect they pin was reported as "the Population dropdown truncates so two
projects read identically", and the first fix, shortening from the left, does not by itself close
it: sibling scratch directories share their last segments, so a fixed depth reproduces the same
collision. Every case here therefore starts from an input that genuinely collides at the default
depth, because a test built on paths that already differ would pass against no implementation at
all.
"""
from c4x.labels import distinct_short_paths, plural, short_path

BACKSLASH = chr(92)

# Two real-shaped scratch workspaces differing only ABOVE the last two segments.
A = "C:/Users/Admin/Roaming/Claude/scratch/aaa11111/run/proj"
B = "C:/Users/Admin/Roaming/Claude/scratch/bbb99999/run/proj"


def test_the_naive_shortening_really_does_collide():
    """The premise. If this ever stops holding, every test below is checking nothing."""
    assert short_path(A) == short_path(B) == ".../run/proj"


def test_short_path_keeps_the_tail_not_the_shared_prefix():
    assert short_path("/a/b/c/d") == ".../c/d"


def test_short_path_leaves_a_path_shorter_than_keep_alone():
    assert short_path("/only") == "/only"
    assert short_path("") == ""
    assert short_path(None) == ""


def test_short_path_handles_the_windows_form_these_arrive_as():
    assert short_path(f"C:{BACKSLASH}a{BACKSLASH}b{BACKSLASH}c") == ".../b/c"


def test_colliding_projects_are_given_enough_path_to_differ():
    out = distinct_short_paths([A, B])
    assert out[A] != out[B]
    assert out[A] == ".../aaa11111/run/proj"
    assert out[B] == ".../bbb99999/run/proj"


def test_only_the_colliding_entries_are_lengthened():
    """A project nobody collides with stays short. Lengthening everything would undo the fix."""
    lonely = "/somewhere/else/alone"
    out = distinct_short_paths([A, B, lonely])
    assert out[lonely] == ".../else/alone"


def test_entries_are_lengthened_by_different_amounts_as_needed():
    one = "X/p/q/one/deep/run/proj"
    two = "X/p/q/two/deep/run/proj"
    three = "X/p/r/two/deep/run/proj"
    out = distinct_short_paths([one, two, three])
    assert len({out[one], out[two], out[three]}) == 3
    # `one` separates from both at one extra segment; the other two need a second.
    assert out[one] == ".../one/deep/run/proj"
    assert out[two] == ".../q/two/deep/run/proj"


def test_two_identical_paths_terminate_instead_of_growing_forever():
    """No depth can separate a value from itself, so the loop must stop rather than run to the
    bound. It returns the full path for both, which is the truthful answer."""
    out = distinct_short_paths([A, A])
    assert out[A]


def test_an_empty_population_is_not_an_error():
    assert distinct_short_paths([]) == {}


def test_the_theme_module_re_exports_the_one_definition():
    """c4x/tabs/summary.py imports short_path from c4x.theme. There must not be a second copy: a
    second copy is what produced this defect, the chart getting the fix and the dropdown not."""
    from c4x.theme import short_path as via_theme
    assert via_theme is short_path


def test_one_of_a_thing_is_singular():
    """The reported string was "1 sessions in project ...", from a cohort holding exactly one."""
    assert plural(1, "session") == "1 session"


def test_more_than_one_is_plural_and_grouped():
    assert plural(2, "session") == "2 sessions"
    assert plural(1234, "session") == "1,234 sessions"


def test_zero_is_plural():
    assert plural(0, "session") == "0 sessions"


def test_an_irregular_plural_can_be_given():
    assert plural(1, "entry", "entries") == "1 entry"
    assert plural(3, "entry", "entries") == "3 entries"


def test_the_population_caption_agrees_with_its_own_count():
    """The caption builder is where this was wrong, so assert it there and not only on the helper.

    A cohort of one is the case that read as broken, and it sits directly beside a session count
    the reader is already being asked to reconcile.
    """
    from unittest.mock import patch as mock_patch

    import c4x.store as store

    with mock_patch.object(store, "cohort_sessions", return_value=["only-one"]):
        assert store.population_label(None, "project::X", "main") == (
            "1 session in project X, main thread only")
    with mock_patch.object(store, "cohort_sessions", return_value=["a", "b"]):
        assert store.population_label(None, "project::X", "main") == (
            "2 sessions in project X, main thread only")


# --- a chat with no folder --------------------------------------------------------------------

SCRATCH = ("C:/Users/Shake/AppData/Roaming/Claude/scratch-workspaces/"
           "54e8e2c2-6d3a-4c51-8ab7-5b84c31e6e4a/5f6eb959-46b3-4fa4-8ce4-79017924010c/"
           "scratch-2026-09-07-433162")
PROJECT = "P:/ClaudeExt/ccxe/c4x"


def test_a_scratch_workspace_is_folderless_and_a_project_is_not():
    from c4x.labels import is_folderless
    assert is_folderless(SCRATCH)
    assert not is_folderless(PROJECT)
    assert not is_folderless(None) and not is_folderless("")


def test_a_backslash_path_is_recognised_too():
    """Every one of these arrives from the store in Windows form."""
    from c4x.labels import is_folderless
    assert is_folderless(SCRATCH.replace("/", BACKSLASH))


def test_a_project_never_gets_a_title_appended():
    """THE NEGATIVE CONTROL. A title is offered and must be ignored: a real path names itself, and
    appending to all of them is the long-label problem short_path exists to solve."""
    from c4x.labels import titled_path
    assert titled_path(PROJECT, {"custom": "SHOULD NOT APPEAR"}) == ".../ccxe/c4x"


def test_a_folderless_chat_shows_its_name():
    from c4x.labels import titled_path
    assert titled_path(SCRATCH, {"last-prompt": "how to create mcp server"}) == (
        ".../scratch-2026-09-07-433162 - how to create mcp server")


def test_it_keeps_one_segment_not_two():
    """The parent of a scratch workspace is a uuid, identical across every one of them on this
    store, so keeping it spends 37 characters saying nothing and pushes the name off the end."""
    from c4x.labels import titled_path
    assert titled_path(SCRATCH, {}) == ".../scratch-2026-09-07-433162"


def test_a_real_title_outranks_the_prompt():
    """A custom title was typed by a person and an ai one was written to BE a title. A last-prompt
    is merely what was said first, so it must never win over either."""
    from c4x.labels import titled_path
    both = {"custom": "Chosen", "ai": "Generated", "last-prompt": "typed first"}
    assert titled_path(SCRATCH, both).endswith(" - Chosen")
    assert titled_path(SCRATCH, {"ai": "Generated", "last-prompt": "x"}).endswith(" - Generated")


def test_a_long_prompt_is_cut_and_marked_as_cut():
    """Measured on the real store: one folder-less chat's prompt is a pasted shell command 70
    characters long. Uncut it would be the whole label."""
    from c4x.labels import PROMPT_LABEL_MAX, titled_path
    long = "stop the service and delete: PS C:/Users/Shake> C:/Users/Shake/WiseFs-thing"
    out = titled_path(SCRATCH, {"last-prompt": long})
    assert out.endswith("...")
    assert len(out.split(" - ", 1)[1]) <= PROMPT_LABEL_MAX + 3


def test_newlines_in_a_prompt_do_not_break_the_label():
    """A prompt is free text and can be multi-line. A label with a newline in it wraps a table row
    open, which is a layout fault rather than a wrong number, but it is still not a label."""
    from c4x.labels import titled_path
    out = titled_path(SCRATCH, {"last-prompt": "first line\n\nsecond line"})
    assert "\n" not in out and " - first line second line" in out


def test_an_empty_or_missing_title_leaves_the_path_alone():
    """Whitespace is not a title. Appending " - " with nothing after it reads as a bug."""
    from c4x.labels import titled_path
    assert titled_path(SCRATCH, {}) == ".../scratch-2026-09-07-433162"
    assert titled_path(SCRATCH, {"last-prompt": "   "}) == ".../scratch-2026-09-07-433162"
    assert titled_path(SCRATCH, None) == ".../scratch-2026-09-07-433162"
