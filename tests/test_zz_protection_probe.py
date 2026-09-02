"""Temporary probe: a deliberately failing test used to prove branch protection blocks a red merge.

Delete this file. It exists only for the duration of one verification pull request.
"""


def test_probe_must_fail():
    assert False, "deliberate failure: branch-protection probe"
