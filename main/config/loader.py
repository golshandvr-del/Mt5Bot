"""
Configuration loader for the MT5 trading bot.

Responsibilities
----------------
- Locate and parse the master YAML config file (config/config.yaml).
- Provide a small, dependency-light dot-access wrapper so the rest of the
  project can read settings as either dict keys or attributes.
- Fall back to a pure-Python YAML-subset parser if PyYAML is not installed,
  so the bot can still boot on a minimal Windows 7 Python environment.

All text in this module is standard ASCII English only.
"""

from __future__ import annotations

import os
from typing import Any, Dict


# Project root = parent of this "config" directory.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.yaml")


def _try_import_yaml():
    """Return the PyYAML module if available, else None."""
    try:
        import yaml  # type: ignore
        return yaml
    except Exception:
        return None


def _minimal_yaml_parse(text: str) -> Dict[str, Any]:
    """
    Extremely small YAML-subset parser used ONLY as a fallback when PyYAML is
    not installed. It supports the constructs actually used by config.yaml:
      - nested mappings via indentation (two spaces per level)
      - simple scalars (int, float, bool, str)
      - inline flow mappings: { key: value, key2: value2 }
      - lists of scalars introduced by "- "
      - comments starting with '#'
    This is NOT a general YAML parser. Install PyYAML for full support.
    """
    import re

    def parse_scalar(token: str) -> Any:
        token = token.strip()
        if token == "" or token == "{}" or token == "[]":
            return {} if token == "{}" else ([] if token == "[]" else "")
        # Strip surrounding quotes.
        if (token.startswith('"') and token.endswith('"')) or (
            token.startswith("'") and token.endswith("'")
        ):
            return token[1:-1]
        low = token.lower()
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
        if low in ("null", "none", "~"):
            return None
        # Numeric?
        try:
            if re.fullmatch(r"[-+]?\d+", token):
                return int(token)
            return float(token)
        except ValueError:
            return token

    def parse_inline_mapping(token: str) -> Dict[str, Any]:
        # token looks like "{ a: 1, b: 2.0 }"
        inner = token.strip()[1:-1].strip()
        result: Dict[str, Any] = {}
        if not inner:
            return result
        # Split on commas that are not inside braces (config has no nesting here).
        parts = [p for p in inner.split(",") if p.strip()]
        for part in parts:
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            result[k.strip()] = parse_scalar(v.strip())
        return result

    # Build a tree using indentation.
    lines = []
    for raw in text.splitlines():
        # Drop comments (naive: comments do not appear inside quoted strings here).
        if "#" in raw:
            # Keep '#' if inside quotes; our config uses no such case.
            raw = raw.split("#", 1)[0]
        if raw.strip() == "":
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip()))

    root: Dict[str, Any] = {}
    # Stack of (indent, container) where container is a dict.
    stack = [(-1, root)]

    i = 0
    while i < len(lines):
        indent, content = lines[i]
        # Pop until parent indent is smaller.
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]

        if content.startswith("- "):
            # List item appended to the last created key's list.
            value = content[2:].strip()
            if not isinstance(parent, list):
                # The parent dict's most recent key should hold a list; handled
                # by mapping branch below. If we reach here, ignore safely.
                i += 1
                continue
            parent.append(parse_scalar(value))
            i += 1
            continue

        if ":" in content:
            key, rest = content.split(":", 1)
            key = key.strip()
            rest = rest.strip()
            if rest == "":
                # Could be a nested mapping or a list. Peek at next line.
                if i + 1 < len(lines):
                    next_indent, next_content = lines[i + 1]
                    if next_indent > indent and next_content.startswith("- "):
                        new_list: list = []
                        parent[key] = new_list
                        stack.append((indent, new_list))
                        i += 1
                        continue
                new_map: Dict[str, Any] = {}
                parent[key] = new_map
                stack.append((indent, new_map))
                i += 1
                continue
            elif rest.startswith("{"):
                parent[key] = parse_inline_mapping(rest)
                i += 1
                continue
            else:
                parent[key] = parse_scalar(rest)
                i += 1
                continue
        i += 1

    return root


class DotDict(dict):
    """
    Dictionary that also allows attribute access and recursive wrapping.

    Example:
        cfg = DotDict({"a": {"b": 1}})
        cfg.a.b -> 1
        cfg["a"]["b"] -> 1
        cfg.get("missing", default) works as normal dict.
    """

    def __getattr__(self, item: str) -> Any:
        try:
            value = self[item]
        except KeyError:
            raise AttributeError(item)
        return DotDict._wrap(value)

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    @staticmethod
    def _wrap(value: Any) -> Any:
        if isinstance(value, dict):
            return DotDict(value)
        if isinstance(value, list):
            return [DotDict._wrap(v) for v in value]
        return value

    def get_path(self, dotted: str, default: Any = None) -> Any:
        """Read a nested value using a dotted path like 'mt5.symbols'."""
        node: Any = self
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return DotDict._wrap(node)


def load_config(path: str = None) -> DotDict:
    """
    Load the master configuration and return a DotDict.

    Parameters
    ----------
    path : str, optional
        Path to a YAML config file. Defaults to config/config.yaml.

    Returns
    -------
    DotDict
        Parsed configuration with attribute access.
    """
    path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        raise FileNotFoundError("Config file not found: %s" % path)

    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    yaml_mod = _try_import_yaml()
    if yaml_mod is not None:
        data = yaml_mod.safe_load(text) or {}
    else:
        data = _minimal_yaml_parse(text)

    if not isinstance(data, dict):
        raise ValueError("Top-level config must be a mapping.")

    cfg = DotDict(data)
    # Attach resolved absolute project root for convenience.
    cfg["project_root"] = PROJECT_ROOT
    return cfg


def resolve_path(cfg: DotDict, relative_or_abs: str) -> str:
    """
    Resolve a path from the config relative to the project root if it is not
    already absolute. Keeps file references portable across machines.
    """
    if not relative_or_abs:
        return relative_or_abs
    if os.path.isabs(relative_or_abs):
        return relative_or_abs
    return os.path.join(cfg.get("project_root", PROJECT_ROOT), relative_or_abs)
