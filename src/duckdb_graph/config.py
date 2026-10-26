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

    def __init__(
        self,
        db_path: str | None = None,
        root: str | None = None,
    ) -> None:
        resolved_db = db_path or _env("DB_PATH") or DEFAULT_DB_PATH
        super().__init__(
            db_path=resolved_db,
            root=root or _env("ROOT"),
        )
