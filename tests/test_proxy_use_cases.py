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
        self.disconnect_count = 0

    def broadcast(self, frame: bytes) -> None:
        self.messages.append(frame)

    def send_snapshot(self, client: object, frames: list[bytes]) -> None:
        self.snapshots.append((client, frames))

    def disconnect_all(self) -> None:
        self.disconnect_count += 1


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


def test_hello_not_forwarded_to_legacy_clients():
    bc = _FakeBroadcaster()
    uc = ProxyUseCases(V2ToV1Mapper(), bc)
    hello = Opcode.HELLO + json.dumps(
        {"type": "hello", "report_protocol": 2, "server": "adn-server", "version": "2.0.0"},
        separators=(",", ":"),
    ).encode()
    for frame in V2ToV1Mapper().translate(Opcode.HELLO, hello[1:]):
        uc.handle_upstream_frame(frame)
    assert not any(msg[:1] == Opcode.HELLO for msg in bc.messages)
    assert any(msg[:1] == Opcode.BRIDGE_SND for msg in bc.messages)
    client = object()
    uc.on_downstream_connected(client)
    assert len(bc.snapshots) == 1
    _, frames = bc.snapshots[0]
    assert all(frame[:1] != Opcode.HELLO for frame in frames)
    assert frames[0][:1] == Opcode.BRIDGE_SND


def test_config_update_rebroadcasts_cached_bridge():
    bc = _FakeBroadcaster()
    uc = ProxyUseCases(V2ToV1Mapper(), bc)
    routing = {
        "type": "routing_table",
        "seq": 1,
        "routes": [
            {
                "relay_table_key": "9990",
                "legs": [
                    {
                        "system": "MASTER-A",
                        "ts": 1,
                        "tgid": 9,
                        "active": False,
                        "to_type": "ON",
                    },
                ],
            },
        ],
    }
    uc.handle_upstream_frame(Opcode.ROUTING_TABLE_SND + json.dumps(routing).encode())
    doc = {
        "type": "dashboard_state",
        "ts": 1.0,
        "ctable": {
            "MASTERS": {
                "MASTER-A": {
                    "mode": "MASTER",
                    "peers": {1001: {"id": 1001, "ts1_static": ["73010"]}},
                },
            },
            "PEERS": {},
            "OPENBRIDGES": {},
        },
    }
    uc.handle_upstream_frame(Opcode.STATE_SND + json.dumps(doc).encode())
    opcodes = [frame[:1] for frame in bc.messages]
    assert opcodes.count(Opcode.CONFIG_SND) == 1
    assert opcodes.count(Opcode.BRIDGE_SND) == 2


def test_snapshot_replay_sends_config_before_bridge():
    bc = _FakeBroadcaster()
    uc = ProxyUseCases(V2ToV1Mapper(), bc)
    uc.handle_upstream_frame(Opcode.BRIDGE_SND + pickle.dumps({"9990": []}))
    uc.handle_upstream_frame(
        Opcode.CONFIG_SND + pickle.dumps({"SYS": {"MODE": "MASTER", "ENABLED": True, "PEERS": {}}})
    )
    client = object()
    uc.on_downstream_connected(client)
    _, frames = bc.snapshots[0]
    assert frames[0][:1] == Opcode.CONFIG_SND
    assert frames[1][:1] == Opcode.BRIDGE_SND


def test_config_master_expansion_disconnects_legacy_clients():
    bc = _FakeBroadcaster()
    uc = ProxyUseCases(V2ToV1Mapper(), bc)
    small = {"SYS-A": {"MODE": "MASTER", "ENABLED": True, "REPEAT": False, "PEERS": {}}}
    uc.handle_upstream_frame(Opcode.CONFIG_SND + pickle.dumps(small))
    assert bc.disconnect_count == 0
    large = {
        **small,
        "SYS-B": {"MODE": "MASTER", "ENABLED": True, "REPEAT": False, "PEERS": {}},
    }
    uc.handle_upstream_frame(Opcode.CONFIG_SND + pickle.dumps(large))
    assert bc.disconnect_count == 1
