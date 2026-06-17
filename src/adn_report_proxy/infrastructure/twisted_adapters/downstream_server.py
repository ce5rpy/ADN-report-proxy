# ADN Report Proxy - downstream TCP server
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

"""TCP server: legacy dashboards connect here (v1 wire)."""

from __future__ import annotations

import logging
from typing import Any

from twisted.internet.protocol import Factory, Protocol
from twisted.protocols.basic import NetstringReceiver

from ...application.proxy_use_cases import ProxyUseCases

logger = logging.getLogger("adn-report-proxy")


class LegacyMonitorProtocol(NetstringReceiver):
    maxLength = 1024 * 1024

    def __init__(self, use_cases: ProxyUseCases) -> None:
        self._use_cases = use_cases

    def connectionMade(self) -> None:
        peer = self.transport.getPeer()
        logger.info("legacy monitor connected from %s:%s", peer.host, peer.port)
        self.factory.active_protocols.add(self)
        self._use_cases.on_downstream_connected(self)

    def connectionLost(self, reason: object = None) -> None:
        self.factory.active_protocols.discard(self)
        super().connectionLost(reason)

    def stringReceived(self, data: bytes) -> None:
        self._use_cases.handle_downstream_request(data, client=self)

    def send_frame(self, frame: bytes) -> None:
        self.sendString(frame)


class DownstreamBroadcasterImpl:
    """Implements DownstreamBroadcaster port."""

    def __init__(self, factory: Factory) -> None:
        self._factory = factory

    def broadcast(self, frame: bytes) -> None:
        for proto in list(self._factory.active_protocols):
            try:
                proto.send_frame(frame)
            except Exception as e:
                logger.debug("broadcast failed: %s", e)

    def send_snapshot(self, client: Any, frames: list[bytes]) -> None:
        if not isinstance(client, LegacyMonitorProtocol):
            return
        for frame in frames:
            client.send_frame(frame)

    def disconnect_all(self) -> None:
        for proto in list(self._factory.active_protocols):
            try:
                proto.transport.loseConnection()
            except Exception as e:
                logger.debug("disconnect failed: %s", e)


class LegacyMonitorFactory(Factory):
    protocol = LegacyMonitorProtocol

    def __init__(self, use_cases: ProxyUseCases | None = None) -> None:
        self._use_cases = use_cases
        self.active_protocols: set[LegacyMonitorProtocol] = set()

    def buildProtocol(self, addr: Any) -> LegacyMonitorProtocol:
        if self._use_cases is None:
            raise RuntimeError("LegacyMonitorFactory.use_cases not wired")
        proto = LegacyMonitorProtocol(self._use_cases)
        proto.factory = self
        return proto
