# ADN Report Proxy - translation state
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

"""Mutable state for v2 delta merge and D-25 bridge synthesis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranslationState:
    """Tracks upstream wire mode and last JSON snapshots for delta patches."""

    upstream_v2: bool = False
    topology_snapshot: dict[str, Any] | None = None
    topology_seq: int = 0
    routing_snapshot: dict[str, Any] | None = None
    routing_seq: int = 0
    bridges_frame_sent: bool = False

    def reset(self) -> None:
        self.upstream_v2 = False
        self.topology_snapshot = None
        self.topology_seq = 0
        self.routing_snapshot = None
        self.routing_seq = 0
        self.bridges_frame_sent = False
