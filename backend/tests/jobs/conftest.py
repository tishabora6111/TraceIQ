from collections.abc import Iterator

import pytest

from tests.jobs.helpers import create_markers_table, drop_markers_table


@pytest.fixture
def markers_table(clean_database: None) -> Iterator[None]:
    create_markers_table()
    try:
        yield
    finally:
        drop_markers_table()
