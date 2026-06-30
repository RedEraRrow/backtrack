"""
Name corpus loader for sort-order heuristics.

Two corpus sizes are available:
  full     — ~9000 given names across all major naming traditions (default)
  minimal  — ~230 names, English + classical; for space-constrained installations

The active corpus is chosen by the 'name_corpus' config key (default: 'full').
Results are cached after first load.
"""

from __future__ import annotations
import json
from pathlib import Path
from functools import lru_cache

_CORPUS_DIR = Path(__file__).parent


@lru_cache(maxsize=2)
def _load(size: str) -> dict:
    filename = 'corpus_minimal.json' if size == 'minimal' else 'corpus_full.json'
    with open(_CORPUS_DIR / filename, encoding='utf-8') as f:
        return json.load(f)


def _corpus() -> dict:
    try:
        from src.config import load_config
        size = load_config().get('name_corpus', 'full')
    except Exception:
        size = 'full'
    return _load(size)


def given_names() -> frozenset[str]:
    return frozenset(_corpus().get('given_names', []))


def compound_surnames() -> list[tuple[str, ...]]:
    return [tuple(c) for c in _corpus().get('compound_surnames', [])]


def honorifics() -> frozenset[str]:
    return frozenset(_corpus().get('honorifics', []))


def ordinal_suffixes() -> frozenset[str]:
    return frozenset(_corpus().get('ordinal_suffixes', []))


def mononyms() -> frozenset[str]:
    """Single-name artists (prince, björk, adele…) — returned as-is, no rearranging."""
    return frozenset(_corpus().get('mononyms', []))


# Module-level aliases for import compatibility.
GIVEN_NAMES       = given_names
COMPOUND_SURNAMES = compound_surnames
HONORIFICS        = honorifics
ORDINAL_SUFFIXES  = ordinal_suffixes
MONONYMS          = mononyms
