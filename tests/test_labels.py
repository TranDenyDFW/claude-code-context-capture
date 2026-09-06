"""c4x.labels: a shortened path still tells one project from another.

These need no store. The defect they pin was reported as "the Population dropdown truncates so two
projects read identically", and the first fix, shortening from the left, does not by itself close
it: sibling scratch directories share their last segments, so a fixed depth reproduces the same
collision. Every case here therefore starts from an input that genuinely collides at the default
depth, because a test built on paths that already differ would pass against no implementation at
all.
"""
from c4x.labels import distinct_short_paths, short_path

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
