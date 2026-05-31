"""
Shared test fixtures for the Store Intelligence test suite.
Sets up an isolated per-session SQLite DB so tests don't share state.
"""
import os
import pytest


@pytest.fixture(autouse=True, scope="session")
def test_db(tmp_path_factory):
    db_file = str(tmp_path_factory.mktemp("db") / "test.db")
    os.environ["DB_PATH"] = db_file
    # Import after env var is set so db.py picks up the right path
    from app.db import init_db
    init_db()
    yield db_file
