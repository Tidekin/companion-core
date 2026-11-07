


def assert_bounded(receipt):
    assert all(0 <= value <= receipt["limits"][name]
               for name, value in receipt["counts"].items())
