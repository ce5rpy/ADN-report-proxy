# ADN Report Proxy - domain errors
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

"""Domain errors — pure, no I/O."""

from __future__ import annotations


class DomainError(Exception):
    """Base domain error."""


class ConfigError(DomainError):
    """Invalid or missing configuration."""


class WireError(DomainError):
    """Report frame encoding/decoding failed."""


class TranslationError(DomainError):
    """v2 payload could not be mapped to v1 wire."""
