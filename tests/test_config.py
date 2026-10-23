"""Settings precedence: constructor > env > default."""

from unittest import mock

from duckdb_graph.config import DEFAULT_DB_PATH, Settings


def test_defaults_apply_when_nothing_set():
    with mock.patch.dict("os.environ", {}, clear=True):
        settings = Settings()
    assert settings.db_path == DEFAULT_DB_PATH
    assert settings.root is None
    assert settings.read_only is False


def test_environment_overrides_default():
    env = {
        "DUCKDB_GRAPH_DB_PATH": "/tmp/env.duckdb",
        "DUCKDB_GRAPH_ROOT": "/tmp/src",
        "DUCKDB_GRAPH_READ_ONLY": "true",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        settings = Settings()
    assert settings.db_path == "/tmp/env.duckdb"
    assert settings.root == "/tmp/src"
    assert settings.read_only is True


def test_constructor_overrides_environment():
    env = {
        "DUCKDB_GRAPH_DB_PATH": "/tmp/env.duckdb",
        "DUCKDB_GRAPH_ROOT": "/tmp/src",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        settings = Settings(db_path="/tmp/explicit.duckdb", root="/tmp/other")
    assert settings.db_path == "/tmp/explicit.duckdb"
    assert settings.root == "/tmp/other"


def test_read_only_truthy_parse():
    for value in ("1", "true", "yes", "TRUE", "Yes"):
        with mock.patch.dict(
            "os.environ", {"DUCKDB_GRAPH_READ_ONLY": value}, clear=True
        ):
            assert Settings().read_only is True
    for value in ("0", "false", "no", ""):
        with mock.patch.dict(
            "os.environ", {"DUCKDB_GRAPH_READ_ONLY": value}, clear=True
        ):
            assert Settings().read_only is False


def test_read_only_constructor_overrides_environment():
    with mock.patch.dict("os.environ", {"DUCKDB_GRAPH_READ_ONLY": "true"}, clear=True):
        assert Settings(read_only=False).read_only is False


def test_root_defaults_to_none():
    with mock.patch.dict("os.environ", {}, clear=True):
        assert Settings().root is None
