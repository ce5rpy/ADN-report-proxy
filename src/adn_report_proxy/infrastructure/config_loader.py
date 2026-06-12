# ADN Report Proxy - YAML config loader
# Copyright (C) 2026  Rodrigo Pérez, CE5RPY <ce5rpy@qmd.cl>
#
###############################################################################
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software Foundation,
#   Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA
###############################################################################

"""YAML configuration loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..domain import ConfigError, Failure, Result, Success


def _str(value: Any, default: str = "") -> str:
    return str(value).strip() if value is not None else default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_config(path: str | Path) -> Result[dict[str, Any], ConfigError]:
    p = Path(path)
    if not p.is_file():
        return Failure(ConfigError(f"config not found: {p}"))
    try:
        with p.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as e:
        return Failure(ConfigError(f"invalid YAML: {e}"))
    if not isinstance(data, dict):
        return Failure(ConfigError("config root must be a mapping"))

    upstream = data.get("UPSTREAM") or {}
    listen = data.get("LISTEN") or {}
    log = data.get("LOGGER") or {}

    return Success({
        "UPSTREAM": {
            "HOST": _str(upstream.get("HOST"), "127.0.0.1"),
            "PORT": _int(upstream.get("PORT"), 4321),
        },
        "LISTEN": {
            "HOST": _str(listen.get("HOST"), ""),
            "PORT": _int(listen.get("PORT"), 4322),
        },
        "LOGGER": {
            "LEVEL": _str(log.get("LOG_LEVEL"), "INFO").upper(),
        },
    })
