"""Configuration resolution for duckdb-graph.

Precedence: explicit constructor argument > environment variable
(``DUCKDB_GRAPH_*``) > default.
"""

import os

from pydantic import BaseModel

ENV_PREFIX = "DUCKDB_GRAPH_"
DEFAULT_DB_PATH = "graph.duckdb"


def _env(name: str) -> str | None:
    """Read an environment variable under the project prefix."""
    return os.environ.get(f"{ENV_PREFIX}{name}")


class Settings(BaseModel):
    """Resolved settings for a code-graph store.

    Each field falls back from the explicit argument to the matching
    ``DUCKDB_GRAPH_*`` environment variable and finally to a default.
    """

    db_path: str
    root: str | None = None
    read_only: bool = False

    def __init__(
        self,
        db_path: str | None = None,
        root: str | None = None,
        read_only: bool | None = None,
    ) -> None:
        resolved_db = db_path or _env("DB_PATH") or DEFAULT_DB_PATH
        if read_only is None:
            env_ro = _env("READ_ONLY")
            resolved_ro = env_ro is not None and env_ro.lower() in {
                "1",
                "true",
                "yes",
            }
        else:
            resolved_ro = read_only
        super().__init__(
            db_path=resolved_db,
            root=root or _env("ROOT"),
            read_only=resolved_ro,
        )
