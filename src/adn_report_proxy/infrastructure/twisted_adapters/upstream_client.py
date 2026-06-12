# ADN Report Proxy - upstream TCP client
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

"""TCP client: connects to adn-server upstream (v1 or v2 wire)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import NetstringReceiver

from ...application.proxy_use_cases import ProxyUseCases
from ...domain import Opcode

if TYPE_CHECKING:
    from ...application.ports import UpstreamCommander

logger = logging.getLogger("adn-report-proxy")


class UpstreamReportProtocol(NetstringReceiver):
    maxLength = 1024 * 1024

    def __init__(self, factory: "UpstreamReportFactory") -> None:
        self._factory = factory

    @property
    def use_cases(self) -> ProxyUseCases:
        uc = self._factory.use_cases
        if uc is None:
            raise RuntimeError("UpstreamReportFactory.use_cases not wired")
        return uc

    def stringReceived(self, data: bytes) -> None:
        self.use_cases.handle_upstream_frame(data)

    def request_state_refresh(self) -> None:
        self.sendString(Opcode.STATE_REQ)

    def request_config_refresh(self) -> None:
        self.sendString(Opcode.CONFIG_REQ)

    def request_bridge_refresh(self) -> None:
        self.sendString(Opcode.BRIDGE_REQ)

    def connectionLost(self, reason: object = None) -> None:
        self._factory.clear_active_protocol()
        self.use_cases.on_upstream_lost()
        super().connectionLost(reason)


class UpstreamCommanderImpl:
    """Implements :class:`UpstreamCommander` using the active upstream protocol."""

    def __init__(self, factory: "UpstreamReportFactory") -> None:
        self._factory = factory

    def request_state_refresh(self) -> None:
        proto = self._factory.active_protocol
        if proto is None:
            logger.debug("STATE_REQ skipped (upstream not connected)")
            return
        proto.request_state_refresh()

    def request_config_refresh(self) -> None:
        proto = self._factory.active_protocol
        if proto is None:
            logger.debug("CONFIG_REQ skipped (upstream not connected)")
            return
        proto.request_config_refresh()

    def request_bridge_refresh(self) -> None:
        proto = self._factory.active_protocol
        if proto is None:
            logger.debug("BRIDGE_REQ skipped (upstream not connected)")
            return
        proto.request_bridge_refresh()


class UpstreamReportFactory(ReconnectingClientFactory):
    protocol = UpstreamReportProtocol
    maxDelay = 30

    def __init__(self, use_cases: ProxyUseCases | None = None) -> None:
        self.use_cases = use_cases
        self.active_protocol: UpstreamReportProtocol | None = None

    def buildProtocol(self, addr: object) -> UpstreamReportProtocol:
        logger.info("upstream connected to adn-server")
        self.resetDelay()
        proto = UpstreamReportProtocol(self)
        self.active_protocol = proto
        return proto

    def clear_active_protocol(self) -> None:
        self.active_protocol = None

    def clientConnectionLost(self, connector: object, reason: object) -> None:
        logger.warning("upstream lost: %s", reason)
        self.clear_active_protocol()
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector: object, reason: object) -> None:
        logger.warning("upstream connect failed: %s", reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)


def make_upstream_commander(factory: UpstreamReportFactory) -> UpstreamCommander:
    return UpstreamCommanderImpl(factory)
