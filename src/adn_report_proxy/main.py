# ADN Report Proxy - composition root
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

"""Composition root: wire ports and start Twisted reactor."""

from __future__ import annotations

import argparse
import logging
import sys

from twisted.internet import reactor

from .application import ProxyUseCases, V2ToV1Mapper
from .domain import is_fail
from .infrastructure.config_loader import load_config
from .infrastructure.logging_setup import create_logger
from .infrastructure.twisted_adapters import (
    DownstreamBroadcasterImpl,
    LegacyMonitorFactory,
    UpstreamReportFactory,
    make_upstream_commander,
)

logger = logging.getLogger("adn-report-proxy")


def run_proxy(config: dict) -> None:
    mapper = V2ToV1Mapper()
    downstream_factory = LegacyMonitorFactory()
    broadcaster = DownstreamBroadcasterImpl(downstream_factory)
    upstream_factory = UpstreamReportFactory(use_cases=None)  # patched below
    upstream_commander = make_upstream_commander(upstream_factory)
    use_cases = ProxyUseCases(mapper, broadcaster, upstream_commander)
    downstream_factory._use_cases = use_cases
    upstream_factory.use_cases = use_cases

    upstream = config["UPSTREAM"]
    listen = config["LISTEN"]
    bind = listen["HOST"] or ""

    reactor.listenTCP(listen["PORT"], downstream_factory, interface=bind)
    reactor.connectTCP(upstream["HOST"], upstream["PORT"], upstream_factory)

    logger.info(
        "listening for legacy monitors on %s:%s; upstream %s:%s",
        bind or "0.0.0.0",
        listen["PORT"],
        upstream["HOST"],
        upstream["PORT"],
    )
    reactor.run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ADN report proxy (v2 upstream → v1 downstream)")
    parser.add_argument("-c", "--config", default="report-proxy.yaml", help="YAML config path")
    args = parser.parse_args(argv)

    result = load_config(args.config)
    if is_fail(result):
        print(f"Config error: {result.error}", file=sys.stderr)
        sys.exit(1)

    create_logger(result.value["LOGGER"]["LEVEL"])
    run_proxy(result.value)


if __name__ == "__main__":
    main()
