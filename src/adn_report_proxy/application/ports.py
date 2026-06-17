# ADN Report Proxy - application ports
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

"""Application ports — abstract interfaces for infrastructure adapters."""

from __future__ import annotations

from typing import Any, Protocol


class V2ToV1Translator(Protocol):
    """Map upstream v2 report payloads to legacy v1 wire frames."""

    @property
    def upstream_is_v2(self) -> bool:
        """True when upstream speaks report wire v2 (JSON)."""

    def translate(self, opcode: bytes, payload: bytes) -> list[bytes]:
        """Return zero or more complete v1 frames (opcode byte + body)."""

    def reset(self) -> None:
        """Clear session state after upstream disconnect."""


class UpstreamCommander(Protocol):
    """Send refresh requests to the upstream report server."""

    def request_state_refresh(self) -> None: ...

    def request_config_refresh(self) -> None: ...

    def request_bridge_refresh(self) -> None: ...


class DownstreamBroadcaster(Protocol):
    """Fan-out translated frames to connected legacy monitors."""

    def broadcast(self, frame: bytes) -> None: ...

    def send_snapshot(self, client: Any, frames: list[bytes]) -> None: ...

    def disconnect_all(self) -> None:
        """Drop legacy monitor TCP sessions (dashboard clears CTABLE on reconnect)."""
        ...


class UpstreamReportHandler(Protocol):
    """Callback surface wired from the upstream TCP client."""

    def on_upstream_frame(self, frame: bytes) -> None: ...

    def on_upstream_lost(self) -> None: ...
