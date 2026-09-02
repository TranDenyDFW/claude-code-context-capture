"""A deliberately failing test, to prove branch protection refuses a red merge.

Not a real test. It exists for the length of one pull request and is deleted with its branch. The
prior review of this plan could report only that GitHub LABELLED the merge blocked, which is a
different claim from the merge being refused, so this exists to execute the merge attempt and
record what comes back.

If this file is ever found on main, the thing it was written to prove has failed.
"""


def test_protection_probe_fails_on_purpose():
    raise AssertionError("deliberate failure: branch-protection probe")
