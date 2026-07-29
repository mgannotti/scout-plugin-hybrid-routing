"""Configuration loading.

Resolution order, first hit wins:

1. explicit path passed on the command line
2. ``$SCOUT_HYBRID_ROUTING_CONFIG``
3. ``~/.scout/hybrid_routing/routing_config.yaml``   (user override)
4. ``<package>/../data/routing_config.yaml``          (shipped default, blank)

The shipped default has every model field blank on purpose. A router that
silently picks a model you did not choose is a router that will eventually
send something somewhere you did not intend.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ENV_CONFIG = "SCOUT_HYBRID_ROUTING_CONFIG"
USER_CONFIG = Path.home() / ".scout" / "hybrid_routing" / "routing_config.yaml"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "data" / "routing_config.yaml"


class ConfigError(RuntimeError):
    """Raised when no usable config can be loaded."""


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"config not found at {path}")
        return path
    env_path = os.environ.get(ENV_CONFIG, "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        if not path.exists():
            raise ConfigError(f"{ENV_CONFIG} points at {path}, which does not exist")
        return path
    if USER_CONFIG.exists():
        return USER_CONFIG
    if DEFAULT_CONFIG.exists():
        return DEFAULT_CONFIG
    raise ConfigError(
        f"no routing config found; expected one at {USER_CONFIG} or {DEFAULT_CONFIG}"
    )


def load_config(explicit: str | None = None) -> tuple[dict, Path]:
    """Load the routing config, returning (config, path_it_came_from)."""
    path = resolve_config_path(explicit)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config at {path} is not valid YAML: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config at {path} is not valid UTF-8: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config at {path} is not a mapping")
    return data, path


def install_user_config(overwrite: bool = False) -> Path:
    """Copy the shipped default to the user override path."""
    if USER_CONFIG.exists() and not overwrite:
        return USER_CONFIG
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    USER_CONFIG.write_text(text, encoding="utf-8")
    return USER_CONFIG
