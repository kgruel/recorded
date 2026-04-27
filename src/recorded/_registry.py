"""Module-level registry: `kind` -> typed-slot adapters + the wrapped function.

Populated by `@recorder` at import time. The read API consults the registry
to rehydrate stored JSON into model instances.

Registry is intentionally process-global. If a function is registered twice
under the same `kind`, the second registration wins (last write) silently.
Test reloads, conditional decorators, and recompiles all routinely produce
duplicate registrations; raising or warning here would be more noise than
signal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ._adapter import Adapter


@dataclass
class RegistryEntry:
    kind: str
    request: Adapter = field(default_factory=Adapter)
    response: Adapter = field(default_factory=Adapter)
    data: Adapter = field(default_factory=Adapter)
    error: Adapter = field(default_factory=Adapter)
    fn: Callable[..., Any] | None = None  # set by decorator (phase 1.2)
    auto_kind: bool = False  # True when kind was auto-derived from fn name


_registry: dict[str, RegistryEntry] = {}


def register(entry: RegistryEntry) -> None:
    _registry[entry.kind] = entry


def lookup(kind: str) -> RegistryEntry | None:
    return _registry.get(kind)


def get_or_passthrough(kind: str) -> RegistryEntry:
    """Return the registered entry for `kind`, or a fresh passthrough entry.

    Used by the read API: rows whose kind has no registered adapters
    rehydrate as raw dicts.
    """
    entry = _registry.get(kind)
    if entry is None:
        return RegistryEntry(kind=kind)
    return entry


def _reset() -> None:
    """Test-only: clear the registry."""
    _registry.clear()
