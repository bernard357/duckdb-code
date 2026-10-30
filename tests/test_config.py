"""Settings precedence: constructor > env > default."""

from unittest import mock

from duckdb_code.config import DEFAULT_DB_PATH, Settings


def test_defaults_apply_when_nothing_set():
    with mock.patch.dict("os.environ", {}, clear=True):
        settings = Settings()
    assert settings.db_path == DEFAULT_DB_PATH
    assert settings.root is None


def test_environment_overrides_default():
    env = {
        "DUCKDB_CODE_DB_PATH": "/tmp/env.duckdb",
        "DUCKDB_CODE_ROOT": "/tmp/src",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        settings = Settings()
    assert settings.db_path == "/tmp/env.duckdb"
    assert settings.root == "/tmp/src"


def test_constructor_overrides_environment():
    env = {
        "DUCKDB_CODE_DB_PATH": "/tmp/env.duckdb",
        "DUCKDB_CODE_ROOT": "/tmp/src",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        settings = Settings(db_path="/tmp/explicit.duckdb", root="/tmp/other")
    assert settings.db_path == "/tmp/explicit.duckdb"
    assert settings.root == "/tmp/other"


def test_root_defaults_to_none():
    with mock.patch.dict("os.environ", {}, clear=True):
        assert Settings().root is None
