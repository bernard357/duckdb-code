"""Content fingerprint used as the incremental re-indexing seam."""

from hashlib import sha256


def compute_etag(content: str) -> str:
    """Return the sha256 fingerprint of a file's content.

    The etag covers exactly the file bytes: an unchanged file keeps its etag,
    and any change flips it. That byte-identity is the incremental-seam signal
    — the indexer compares a file's stored etag against its freshly computed one
    to decide whether the file's contribution can be reused or must be rebuilt.
    """
    return sha256(content.encode("utf-8")).hexdigest()
