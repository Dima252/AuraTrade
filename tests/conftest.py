from collections.abc import Iterator

import pytest
import structlog

pytest_plugins = ["tests.pg"]


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    # configure_logging() binds a stream globally; don't let it leak between tests.
    yield
    structlog.reset_defaults()
