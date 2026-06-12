# ADN Report Proxy - v2 to v1 mapper tests
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

"""Tests for v2 → v1 wire translation."""

from __future__ import annotations

import json
import pickle

from adn_report_proxy.application.v2_to_v1_mapper import V2ToV1Mapper
from adn_report_proxy.domain import Opcode


def _sample_dashboard_state() -> dict:
    return {
        "type": "dashboard_state",
        "ts": 1717555200.0,
        "ctable": {
            "MASTERS": {
                "MASTER-A": {
                    "mode": "MASTER",
                    "ip": "10.0.0.1",
                    "port": 62030,
                    "peers": {
                        1001: {"id": 1001, "ip": "10.0.0.2", "port": 62031, "callsign": "HS1TEST"},
                    },
                },
            },
            "PEERS": {},
            "OPENBRIDGES": {
                "OBP-CL": {"ip": "10.0.0.3", "port": 62032, "network_id": 260210},
            },
        },
    }


def test_hello_v2_to_v1_includes_empty_bridges():
    mapper = V2ToV1Mapper()
    body = json.dumps(
        {"type": "hello", "report_protocol": 2, "server": "adn-server", "version": "2.0.0"},
        separators=(",", ":"),
    ).encode()
    frames = mapper.translate(Opcode.HELLO, body)
    assert len(frames) == 2
    doc = json.loads(frames[0][1:].decode())
    assert doc["protocol"] == 1
    assert doc["server"] == "adn-server"
    assert frames[1][:1] == Opcode.BRIDGE_SND
    assert pickle.loads(frames[1][1:]) == {}


def test_dashboard_state_to_config_snd():
    mapper = V2ToV1Mapper()
    body = json.dumps(_sample_dashboard_state()).encode()
    frames = mapper.translate(Opcode.STATE_SND, body)
    assert len(frames) == 1
    assert frames[0][:1] == Opcode.CONFIG_SND
    config = pickle.loads(frames[0][1:])
    assert "MASTER-A" in config
    assert "OBP-CL" in config
    assert config["MASTER-A"]["MODE"] == "MASTER"


def test_voice_event_to_brdg_event():
    mapper = V2ToV1Mapper()
    voice = {
        "type": "voice_event",
        "call_family": "GROUP",
        "phase": "END",
        "direction": "RX",
        "system": "OBP-CL",
        "stream_id": 42,
        "peer_id": 0,
        "src_id": 1234567,
        "slot": 1,
        "dst_id": 9,
        "duration_s": 12.5,
    }
    frames = mapper.translate(Opcode.VOICE_EVENT_SND, json.dumps(voice).encode())
    assert len(frames) == 1
    assert frames[0][:1] == Opcode.BRDG_EVENT
    csv = frames[0][1:].decode()
    assert csv.startswith("GROUP VOICE,END,RX,OBP-CL")
    assert csv.endswith("12.50")


def test_v1_passthrough():
    mapper = V2ToV1Mapper()
    systems = {"SYS": {"MODE": "MASTER", "ENABLED": True, "PEERS": {}}}
    body = pickle.dumps(systems)
    frames = mapper.translate(Opcode.CONFIG_SND, body)
    assert frames == [Opcode.CONFIG_SND + body]
    assert mapper.upstream_is_v2 is False


def test_routing_table_relay_table_key_to_bridge_snd():
    mapper = V2ToV1Mapper()
    routing = {
        "type": "routing_table",
        "seq": 1,
        "routes": [
            {
                "relay_table_key": "1001:9:1",
                "legs": [
                    {
                        "system": "MASTER-A",
                        "ts": 1,
                        "tgid": 9,
                        "active": True,
                        "to_type": "ON",
                    },
                ],
            },
        ],
    }
    frames = mapper.translate(Opcode.ROUTING_TABLE_SND, json.dumps(routing).encode())
    assert len(frames) == 1
    assert frames[0][:1] == Opcode.BRIDGE_SND
    bridges = pickle.loads(frames[0][1:])
    assert "1001:9:1" in bridges
    assert bridges["1001:9:1"][0]["SYSTEM"] == "MASTER-A"
    assert bridges["1001:9:1"][0]["ACTIVE"] is True


def test_dashboard_state_maps_peer_options_and_static_tgs():
    mapper = V2ToV1Mapper()
    doc = {
        "type": "dashboard_state",
        "ts": 1717555200.0,
        "ctable": {
            "MASTERS": {
                "MASTER-A": {
                    "mode": "MASTER",
                    "single_mode": True,
                    "default_ua_timer": 12.5,
                    "peers": {
                        1001: {
                            "id": 1001,
                            "ip": "10.0.0.2",
                            "port": 62031,
                            "options": "TS1_STATIC=9,10;SINGLE=1",
                            "ts1_static": ["9", "10"],
                            "ua_timer_min": 5.0,
                            "ua_sessions": {"9": 123.0},
                        },
                    },
                },
            },
            "PEERS": {},
            "OPENBRIDGES": {},
        },
    }
    frames = mapper.translate(Opcode.STATE_SND, json.dumps(doc).encode())
    config = pickle.loads(frames[0][1:])
    master = config["MASTER-A"]
    assert master["SINGLE_MODE"] is True
    assert master["DEFAULT_UA_TIMER"] == 12.5
    peer_key = (1001).to_bytes(4, "big")
    peer = master["PEERS"][peer_key]
    assert peer["TS1_STATIC"] == "9,10"
    assert peer["OPTIONS"] == b"TS1_STATIC=9,10;SINGLE=1"
    assert peer["UA_TIMER_MIN"] == 5.0
    assert peer["UA_SESSIONS"] == {"9": 123.0}
