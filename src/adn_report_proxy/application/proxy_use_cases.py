# ADN Report Proxy - proxy use cases
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

"""Orchestrate translation and snapshot replay for legacy downstream clients."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..domain import Opcode
from .ports import DownstreamBroadcaster, UpstreamCommander, V2ToV1Translator

logger = logging.getLogger("adn-report-proxy")

# Opcodes legacy monitors need on (re)connect before live events.
_SNAPSHOT_OPCODES = frozenset({b"\xff", b"\x01", b"\x03"})

_DOWNSTREAM_REFRESH_OPCODES = frozenset({
    Opcode.CONFIG_REQ,
    Opcode.BRIDGE_REQ,
})

# Optional background upstream refresh when cache is served but stale.
_STALE_REFRESH_SECONDS = 30.0


@dataclass
class ProxyState:
    """Last v1 frames to replay when a legacy monitor connects."""

    snapshot: list[bytes] = field(default_factory=list)
    last_cache_update_at: float = 0.0
    last_upstream_refresh_at: float = 0.0


class ProxyUseCases:
    """Application use case: upstream frame → translate → broadcast + cache."""

    def __init__(
        self,
        translator: V2ToV1Translator,
        broadcaster: DownstreamBroadcaster,
        upstream: UpstreamCommander | None = None,
        *,
        stale_refresh_seconds: float = _STALE_REFRESH_SECONDS,
    ) -> None:
        self._translator = translator
        self._broadcaster = broadcaster
        self._upstream = upstream
        self._stale_refresh_seconds = stale_refresh_seconds
        self.state = ProxyState()

    def handle_upstream_frame(self, frame: bytes) -> None:
        if not frame:
            return
        opcode = frame[:1]
        payload = frame[1:]
        out_frames = self._translator.translate(opcode, payload)
        for out in out_frames:
            self._maybe_cache(out)
            self._broadcaster.broadcast(out)

    def handle_downstream_request(self, frame: bytes, client: object | None = None) -> None:
        """Serve CONFIG/BRIDGE from cache; refresh upstream only on cache miss or stale TTL."""
        if not frame or frame[:1] not in _DOWNSTREAM_REFRESH_OPCODES:
            return
        req = frame[:1]
        if req == Opcode.CONFIG_REQ:
            cached = self._cached_frame(Opcode.CONFIG_SND)
        else:
            cached = self._cached_frame(Opcode.BRIDGE_SND)

        if cached is not None and client is not None:
            self._broadcaster.send_snapshot(client, [cached])
            logger.debug("legacy %s served from cache", req.hex())
            self._maybe_refresh_upstream(stale_only=True)
            return

        if self._upstream is None:
            logger.debug("legacy %s ignored (no upstream, cache empty)", req.hex())
            return

        logger.debug("legacy %s cache miss → upstream refresh", req.hex())
        self._request_upstream_refresh(req)
        self._mark_upstream_refresh()

    def snapshot_for_client(self) -> list[bytes]:
        return list(self.state.snapshot)

    def on_downstream_connected(self, client: object) -> None:
        frames = self.snapshot_for_client()
        if frames:
            self._broadcaster.send_snapshot(client, frames)
            logger.info("replayed %d cached v1 frame(s) to new legacy client", len(frames))
        else:
            logger.info("legacy client connected; no snapshot cached yet")

    def on_upstream_lost(self) -> None:
        logger.warning("upstream server connection lost; clearing snapshot cache")
        self._translator.reset()
        self.state.snapshot.clear()
        self.state.last_cache_update_at = 0.0
        self.state.last_upstream_refresh_at = 0.0

    def _cached_frame(self, opcode: bytes) -> bytes | None:
        for frame in reversed(self.state.snapshot):
            if frame[:1] == opcode:
                return frame
        return None

    def _request_upstream_refresh(self, req: bytes) -> None:
        if self._upstream is None:
            return
        if self._translator.upstream_is_v2:
            self._upstream.request_state_refresh()
            return
        if req == Opcode.CONFIG_REQ:
            self._upstream.request_config_refresh()
        else:
            self._upstream.request_bridge_refresh()

    def _maybe_refresh_upstream(self, *, stale_only: bool) -> None:
        if self._upstream is None:
            return
        if stale_only:
            if self.state.last_cache_update_at <= 0:
                return
            age = time.monotonic() - self.state.last_cache_update_at
            if age < self._stale_refresh_seconds:
                return
        self._request_upstream_refresh(Opcode.CONFIG_REQ)
        self._mark_upstream_refresh()
        logger.debug("background upstream refresh (stale_only=%s)", stale_only)

    def _mark_upstream_refresh(self) -> None:
        self.state.last_upstream_refresh_at = time.monotonic()

    def _maybe_cache(self, frame: bytes) -> None:
        if not frame:
            return
        op = frame[:1]
        if op not in _SNAPSHOT_OPCODES:
            return
        if op == b"\xff":
            self.state.snapshot = [f for f in self.state.snapshot if f[:1] != b"\xff"] + [frame]
        elif op == b"\x01":
            self.state.snapshot = [f for f in self.state.snapshot if f[:1] != b"\x01"] + [frame]
        elif op == b"\x03":
            self.state.snapshot = [f for f in self.state.snapshot if f[:1] != b"\x03"] + [frame]
        self.state.last_cache_update_at = time.monotonic()
