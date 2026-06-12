# ADN Report Proxy - proxy use cases tests
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

"""Tests for snapshot caching and broadcast orchestration."""

from __future__ import annotations

import json
import pickle

from adn_report_proxy.application.proxy_use_cases import ProxyUseCases
from adn_report_proxy.application.v2_to_v1_mapper import V2ToV1Mapper
from adn_report_proxy.domain import Opcode


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.snapshots: list[tuple[object, list[bytes]]] = []

    def broadcast(self, frame: bytes) -> None:
        self.messages.append(frame)

    def send_snapshot(self, client: object, frames: list[bytes]) -> None:
        self.snapshots.append((client, frames))


class _FakeUpstream:
    def __init__(self) -> None:
        self.state_refresh_count = 0
        self.config_refresh_count = 0
        self.bridge_refresh_count = 0

    def request_state_refresh(self) -> None:
        self.state_refresh_count += 1

    def request_config_refresh(self) -> None:
        self.config_refresh_count += 1

    def request_bridge_refresh(self) -> None:
        self.bridge_refresh_count += 1


def test_downstream_config_req_served_from_cache_without_upstream():
    bc = _FakeBroadcaster()
    upstream = _FakeUpstream()
    uc = ProxyUseCases(V2ToV1Mapper(), bc, upstream, stale_refresh_seconds=3600.0)
    config = {"SYS": {"MODE": "MASTER", "ENABLED": True, "PEERS": {}}}
    uc.handle_upstream_frame(Opcode.CONFIG_SND + pickle.dumps(config))
    client = object()
    uc.handle_downstream_request(Opcode.CONFIG_REQ, client=client)
    assert upstream.state_refresh_count == 0
    assert upstream.config_refresh_count == 0
    assert len(bc.snapshots) == 1
    _, frames = bc.snapshots[0]
    assert frames[0][:1] == Opcode.CONFIG_SND
    assert pickle.loads(frames[0][1:]) == config


def test_downstream_config_req_cache_miss_triggers_v2_state_req():
    bc = _FakeBroadcaster()
    upstream = _FakeUpstream()
    mapper = V2ToV1Mapper()
    hello = json.dumps(
        {"type": "hello", "report_protocol": 2, "server": "adn-server", "version": "2.0.0"},
        separators=(",", ":"),
    ).encode()
    mapper.translate(Opcode.HELLO, hello)
    uc = ProxyUseCases(mapper, bc, upstream)
    uc.handle_downstream_request(Opcode.CONFIG_REQ, client=object())
    assert upstream.state_refresh_count == 1
    assert upstream.config_refresh_count == 0


def test_downstream_config_req_cache_miss_triggers_v1_config_req():
    bc = _FakeBroadcaster()
    upstream = _FakeUpstream()
    mapper = V2ToV1Mapper()
    uc = ProxyUseCases(mapper, bc, upstream)
    body = pickle.dumps({"SYS": {"MODE": "MASTER", "ENABLED": True}})
    # Establish v1 upstream mode without caching CONFIG (translate only, no handle_upstream_frame).
    mapper.translate(Opcode.CONFIG_SND, body)
    uc.handle_downstream_request(Opcode.CONFIG_REQ, client=object())
    assert upstream.config_refresh_count == 1
    assert upstream.state_refresh_count == 0


def test_caches_hello_and_config_for_replay():
    bc = _FakeBroadcaster()
    uc = ProxyUseCases(V2ToV1Mapper(), bc)
    hello = Opcode.HELLO + json.dumps(
        {"type": "hello", "report_protocol": 2, "server": "adn-server", "version": "2.0.0"},
        separators=(",", ":"),
    ).encode()
    for frame in V2ToV1Mapper().translate(Opcode.HELLO, hello[1:]):
        uc.handle_upstream_frame(frame)
    client = object()
    uc.on_downstream_connected(client)
    assert len(bc.snapshots) == 1
    _, frames = bc.snapshots[0]
    assert frames[0][:1] == Opcode.HELLO
    assert json.loads(frames[0][1:].decode())["protocol"] == 1
