"""Import smoke test — keeps `pytest` from exiting 5 ("no tests collected") on a bare
scaffold, and fails loudly if the package stops importing cleanly."""

import ecosystem_statistics


def test_package_imports() -> None:
    assert ecosystem_statistics.__version__
