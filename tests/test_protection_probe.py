"""DELIBERATELY FAILING, and temporary.

It exists to prove that branch protection on `main` refuses a merge while `suite` is red. A
protection object returned by the API is a claim about configuration; a blocked merge button is
the thing that actually matters, and the only way to see one is to produce a red check.

This file is deleted with its branch. If you are reading it on any branch that is not
`probe/protection-blocks-red`, something went wrong: delete it.
"""


def test_this_fails_on_purpose_to_prove_the_merge_is_blocked():
    assert False, "deliberate failure, see this module's docstring"
