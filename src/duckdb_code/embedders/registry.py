"""Resolve a backend string to an :class:`Embedder` instance.

A backend string is either a bare name (``"fastembed"``, ``"hashing"``) or a
``name:variant`` pair (``"fastembed:BAAI/bge-small-en-v1.5"``). Factories are
registered by name; the variant (the part after the first colon) is passed
through to the factory, so heavy backends are imported only when used.
"""

from collections.abc import Callable

from duckdb_code.embedders.base import Embedder
from duckdb_code.embedders.hashing import HashingEmbedder

EmbedderFactory = Callable[[str | None], Embedder]

_REGISTRY: dict[str, EmbedderFactory] = {}


def register_embedder(name: str, factory: EmbedderFactory) -> None:
    """Register a backend factory under ``name``.

    The factory receives the variant string (the part after the first colon in
    the backend spec) or ``None`` when no variant is given.
    """
    _REGISTRY[name] = factory


def resolve_embedder(spec: str) -> Embedder:
    """Resolve a backend spec such as ``"fastembed"`` or ``"hashing:128"``.

    Raises ``ValueError`` for an unknown backend name, listing the registered
    names to aid discovery.
    """
    name, _, variant = spec.partition(":")
    factory = _REGISTRY.get(name)
    if factory is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(
            f"Unknown embedding backend '{name}'. Registered backends: {known}."
        )
    return factory(variant or None)


def _hashing_factory(variant: str | None) -> Embedder:
    if variant:
        return HashingEmbedder(dimensions=int(variant))
    return HashingEmbedder()


def _fastembed_factory(variant: str | None) -> Embedder:
    from duckdb_code.embedders.fastembed import FastembedEmbedder

    return FastembedEmbedder(model_name=variant) if variant else FastembedEmbedder()


register_embedder("hashing", _hashing_factory)
register_embedder("fastembed", _fastembed_factory)
