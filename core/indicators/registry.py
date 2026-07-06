"""
Indicator registry: makes indicators pluggable.

Indicators register themselves via the @register_indicator decorator. The
factory build_enabled_indicators(cfg) reads config/config.yaml -> indicators
and instantiates only the enabled ones with their configured params.

This decoupling means you can add a new indicator file, decorate the class,
list it in config.yaml, and it becomes available to the strategy/decision
layers without touching any other code.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Type

from core.indicators.base import Indicator


# Global registry mapping indicator name -> class.
_REGISTRY: Dict[str, Type[Indicator]] = {}


def register_indicator(cls: Type[Indicator]) -> Type[Indicator]:
    """Class decorator that adds an Indicator subclass to the registry."""
    name = getattr(cls, "name", None)
    if not name or name == "base":
        raise ValueError("Indicator class must define a unique 'name'.")
    _REGISTRY[name] = cls
    return cls


def get_indicator_class(name: str) -> Type[Indicator]:
    """Return the registered class for an indicator name."""
    if name not in _REGISTRY:
        raise KeyError("Unknown indicator: %s" % name)
    return _REGISTRY[name]


def list_indicators() -> List[str]:
    """Return the sorted names of all registered indicators."""
    return sorted(_REGISTRY.keys())


def build_enabled_indicators(cfg: Any) -> Dict[str, Indicator]:
    """
    Instantiate every indicator that is enabled in config.yaml -> indicators.

    Returns a dict {name: indicator_instance}. Unknown names in config are
    skipped with no error so the config and code can evolve independently.
    """
    result: Dict[str, Indicator] = {}
    ind_cfg = cfg.get("indicators", {}) if hasattr(cfg, "get") else {}
    for name, spec in ind_cfg.items():
        try:
            enabled = bool(spec.get("enabled", False)) if hasattr(spec, "get") else False
        except Exception:
            enabled = False
        if not enabled:
            continue
        if name not in _REGISTRY:
            # Indicator listed in config but not implemented/registered yet.
            continue
        params = {}
        if hasattr(spec, "get"):
            params = spec.get("params", {}) or {}
        try:
            result[name] = _REGISTRY[name](params=dict(params))
        except Exception:
            # Never let one bad indicator break the whole build.
            continue
    return result


def build_all_indicators() -> Dict[str, Indicator]:
    """Instantiate every registered indicator with default params."""
    return {name: cls() for name, cls in _REGISTRY.items()}
