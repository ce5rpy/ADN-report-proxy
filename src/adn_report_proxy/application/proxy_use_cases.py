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
import pickle
import time
from dataclasses import dataclass, field

from ..domain import Opcode
from .ports import DownstreamBroadcaster, UpstreamCommander, V2ToV1Translator
from .v2_to_v1_mapper import merge_ua_sessions_into_bridges
from .wire_log import (
    opcode_name,
    summarize_legacy_out_frames,
    summarize_legacy_request,
    summarize_snapshot,
    summarize_upstream,
)

logger = logging.getLogger("adn-report-proxy")

# Opcodes legacy monitors need on (re)connect before live events.
# HELLO (0xFF) is v2-only; legacy adn-dmr-server / legacy dashboard never used it.
_SNAPSHOT_OPCODES = frozenset({b"\x01", b"\x03"})

# Legacy dashboard builds CTABLE from CONFIG before applying BRIDGE (build_tgstats).
_SNAPSHOT_ORDER = {Opcode.CONFIG_SND: 0, Opcode.BRIDGE_SND: 1}

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
    broadcast_masters: frozenset[str] = field(default_factory=frozenset)


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
        logger.info("upstream recv: %s", summarize_upstream(opcode, payload))
        out_frames = self._translator.translate(opcode, payload)
        if out_frames:
            logger.info("legacy send: %s", summarize_legacy_out_frames(out_frames))
        elif opcode not in (Opcode.HELLO,):
            logger.debug("upstream %s → no legacy frames", opcode_name(opcode))
        if opcode == Opcode.HELLO and self._translator.upstream_is_v2 and self._upstream is not None:
            logger.info("upstream send: BRIDGE_REQ (bootstrap routing after v2 HELLO)")
            self._upstream.request_bridge_refresh()
        config_frame: bytes | None = None
        for out in out_frames:
            if out[:1] == Opcode.HELLO:
                continue
            if out[:1] == Opcode.CONFIG_SND:
                config_frame = out
                continue
            self._maybe_cache(out)
            self._broadcaster.broadcast(out)
        if config_frame is not None:
            self._refresh_legacy_static_tg_view(config_frame)

    def _refresh_legacy_static_tg_view(self, config_frame: bytes) -> None:
        """Legacy dashboard: ``build_tgstats`` runs on BRIDGE_SND only; ``build_stats`` on CONFIG_SND."""
        config = pickle.loads(config_frame[1:])
        bridge = self._cached_frame(Opcode.BRIDGE_SND)
        if bridge is not None:
            bridges = pickle.loads(bridge[1:])
            enriched = merge_ua_sessions_into_bridges(bridges, config)
            bridge_frame = Opcode.BRIDGE_SND + pickle.dumps(enriched)
            self._maybe_cache(bridge_frame)
            self._broadcaster.broadcast(bridge_frame)
        self._publish_config(config_frame)

    def _publish_config(self, config_frame: bytes) -> None:
        """Broadcast CONFIG; reset clients when master keys change (legacy update_hblink_table)."""
        masters = self._config_master_names(config_frame)
        prev = self.state.broadcast_masters
        self.state.broadcast_masters = masters
        if prev and masters != prev:
            logger.info(
                "legacy CONFIG master set changed (%d → %d names) → reset downstream clients",
                len(prev),
                len(masters),
            )
            self._broadcaster.disconnect_all()
        self._maybe_cache(config_frame)
        self._broadcaster.broadcast(config_frame)

    @staticmethod
    def _config_master_names(config_frame: bytes) -> frozenset[str]:
        try:
            config = pickle.loads(config_frame[1:])
        except Exception:
            return frozenset()
        if not isinstance(config, dict):
            return frozenset()
        return frozenset(
            str(name)
            for name, entry in config.items()
            if isinstance(entry, dict) and entry.get("MODE") == "MASTER"
        )

    def handle_downstream_request(self, frame: bytes, client: object | None = None) -> None:
        """Serve CONFIG/BRIDGE from cache; refresh upstream only on cache miss or stale TTL."""
        if not frame:
            return
        req = frame[:1]
        if req not in _DOWNSTREAM_REFRESH_OPCODES:
            logger.debug("legacy recv ignored: %s (%d B)", opcode_name(req), len(frame))
            return
        req_label = summarize_legacy_request(req)
        if req == Opcode.CONFIG_REQ:
            cached = self._cached_frame(Opcode.CONFIG_SND)
            rsp_label = "CONFIG_SND"
        else:
            cached = self._cached_frame(Opcode.BRIDGE_SND)
            rsp_label = "BRIDGE_SND"

        if cached is not None and client is not None:
            self._broadcaster.send_snapshot(client, [cached])
            logger.info(
                "legacy request: %s → replied %s from cache (%s)",
                req_label,
                rsp_label,
                summarize_upstream(cached[:1], cached[1:]),
            )
            self._maybe_refresh_upstream(stale_only=True)
            return

        if self._upstream is None:
            logger.info("legacy request: %s → no reply (no upstream, cache empty)", req_label)
            return

        upstream_req = "STATE_REQ" if self._translator.upstream_is_v2 else req_label
        logger.info(
            "legacy request: %s → cache miss, upstream send %s",
            req_label,
            upstream_req,
        )
        self._request_upstream_refresh(req)
        self._mark_upstream_refresh()

    def snapshot_for_client(self) -> list[bytes]:
        return sorted(
            self.state.snapshot,
            key=lambda frame: _SNAPSHOT_ORDER.get(frame[:1], 99),
        )

    def on_downstream_connected(self, client: object) -> None:
        frames = self.snapshot_for_client()
        if frames:
            self._broadcaster.send_snapshot(client, frames)
            logger.info(
                "legacy connect → replay snapshot: %s (%d frame(s))",
                summarize_snapshot(frames),
                len(frames),
            )
        else:
            logger.info("legacy connect → no snapshot cached yet")

    def on_upstream_lost(self) -> None:
        logger.warning("upstream server connection lost; keeping legacy snapshot cache")
        self._translator.on_upstream_lost()

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
        logger.info("upstream send: STATE_REQ (stale cache background refresh)")

    def _mark_upstream_refresh(self) -> None:
        self.state.last_upstream_refresh_at = time.monotonic()

    def _maybe_cache(self, frame: bytes) -> None:
        if not frame:
            return
        op = frame[:1]
        if op not in _SNAPSHOT_OPCODES:
            return
        if op == b"\x01":
            self.state.snapshot = [f for f in self.state.snapshot if f[:1] != b"\x01"] + [frame]
        elif op == b"\x03":
            self.state.snapshot = [f for f in self.state.snapshot if f[:1] != b"\x03"] + [frame]
        self.state.last_cache_update_at = time.monotonic()
