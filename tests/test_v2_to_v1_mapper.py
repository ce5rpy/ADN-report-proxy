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

from dmr_utils3.utils import int_id

from adn_report_proxy.application.translation_state import TranslationState
from adn_report_proxy.application.v2_to_v1_mapper import (
    V2ToV1Mapper,
    dashboard_state_to_config,
    merge_ua_sessions_into_bridges,
    topology_to_config,
)
from adn_report_proxy.domain import Opcode


def _seed_routing_masters(mapper: V2ToV1Mapper, *names: str) -> None:
    legs = [
        {"system": name, "ts": 1, "tgid": 9, "active": False, "to_type": "ON"}
        for name in names
    ]
    routing = {
        "type": "routing_table",
        "seq": 1,
        "routes": [{"relay_table_key": "9990", "legs": legs}],
    }
    mapper.translate(Opcode.ROUTING_TABLE_SND, json.dumps(routing).encode())


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
    _seed_routing_masters(mapper, "MASTER-A")
    body = json.dumps(_sample_dashboard_state()).encode()
    frames = mapper.translate(Opcode.STATE_SND, body)
    assert len(frames) == 1
    assert frames[0][:1] == Opcode.CONFIG_SND
    config = pickle.loads(frames[0][1:])
    assert "MASTER-A" in config
    assert "OBP-CL" in config
    assert config["MASTER-A"]["MODE"] == "MASTER"
    assert config["MASTER-A"]["REPEAT"] is False
    assert config["OBP-CL"]["TARGET_IP"] == "10.0.0.3"
    assert config["OBP-CL"]["TARGET_PORT"] == 62032
    peer = config["MASTER-A"]["PEERS"][(1001).to_bytes(4, "big")]
    assert peer["CALLSIGN"] == "HS1TEST"
    assert "PACKAGE_ID" in peer
    assert float(peer["LATITUDE"].decode()) == 0.0
    assert float(peer["LONGITUDE"].decode()) == 0.0


def test_dashboard_state_master_repeat_from_json():
    mapper = V2ToV1Mapper()
    _seed_routing_masters(mapper, "MASTER-A")
    doc = _sample_dashboard_state()
    doc["ctable"]["MASTERS"]["MASTER-A"]["repeat"] = True
    frames = mapper.translate(Opcode.STATE_SND, json.dumps(doc).encode())
    config = pickle.loads(frames[0][1:])
    assert config["MASTER-A"]["REPEAT"] is True


def test_topology_master_repeat_defaults_false():
    mapper = V2ToV1Mapper()
    topo = {
        "type": "topology",
        "seq": 1,
        "systems": [
            {
                "name": "MASTER-A",
                "mode": "MASTER",
                "enabled": True,
                "peers": [],
            }
        ],
    }
    frames = mapper.translate(Opcode.TOPOLOGY_SND, json.dumps(topo).encode())
    config = pickle.loads(frames[0][1:])
    assert config["MASTER-A"]["REPEAT"] is False


def test_dashboard_state_master_static_tgs_from_topology_cache():
    mapper = V2ToV1Mapper()
    topo = {
        "type": "topology",
        "seq": 1,
        "systems": [
            {
                "name": "MASTER-A",
                "mode": "MASTER",
                "enabled": True,
                "ts1_static": ["73010"],
                "ts2_static": ["52090"],
                "peers": [],
            }
        ],
    }
    mapper.translate(Opcode.TOPOLOGY_SND, json.dumps(topo).encode())
    doc = {
        "type": "dashboard_state",
        "ts": 1.0,
        "ctable": {
            "MASTERS": {
                "MASTER-A": {
                    "mode": "MASTER",
                    "peers": {1001: {"id": 1001, "ip": "10.0.0.2"}},
                },
            },
            "PEERS": {},
            "OPENBRIDGES": {},
        },
    }
    config = pickle.loads(mapper.translate(Opcode.STATE_SND, json.dumps(doc).encode())[0][1:])
    assert config["MASTER-A"]["TS1_STATIC"] == "73010"
    assert config["MASTER-A"]["TS2_STATIC"] == "52090"


def test_dashboard_state_master_static_tgs_from_peer_options():
    mapper = V2ToV1Mapper()
    _seed_routing_masters(mapper, "MASTER-A")
    doc = {
        "type": "dashboard_state",
        "ts": 1.0,
        "ctable": {
            "MASTERS": {
                "MASTER-A": {
                    "mode": "MASTER",
                    "peers": {
                        1001: {
                            "id": 1001,
                            "options": "TS1=73010,73011;TS2=52090",
                        },
                    },
                },
            },
            "PEERS": {},
            "OPENBRIDGES": {},
        },
    }
    config = pickle.loads(mapper.translate(Opcode.STATE_SND, json.dumps(doc).encode())[0][1:])
    assert config["MASTER-A"]["TS1_STATIC"] == "73010,73011"
    assert config["MASTER-A"]["TS2_STATIC"] == "52090"


def test_dashboard_state_master_static_tgs_from_peers():
    mapper = V2ToV1Mapper()
    _seed_routing_masters(mapper, "MASTER-A")
    doc = {
        "type": "dashboard_state",
        "ts": 1717555200.0,
        "ctable": {
            "MASTERS": {
                "MASTER-A": {
                    "mode": "MASTER",
                    "peers": {
                        1001: {
                            "id": 1001,
                            "ip": "10.0.0.2",
                            "port": 62031,
                            "ts1_static": ["73010", "73011"],
                            "ts2_static": ["52090"],
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
    assert config["MASTER-A"]["TS1_STATIC"] == "73010,73011"
    assert config["MASTER-A"]["TS2_STATIC"] == "52090"


def test_topology_master_static_tgs():
    mapper = V2ToV1Mapper()
    topo = {
        "type": "topology",
        "seq": 1,
        "systems": [
            {
                "name": "MASTER-A",
                "mode": "MASTER",
                "enabled": True,
                "ts1_static": ["91", "92"],
                "ts2_static": ["730"],
                "peers": [],
            }
        ],
    }
    frames = mapper.translate(Opcode.TOPOLOGY_SND, json.dumps(topo).encode())
    config = pickle.loads(frames[0][1:])
    assert config["MASTER-A"]["TS1_STATIC"] == "91,92"
    assert config["MASTER-A"]["TS2_STATIC"] == "730"


def test_voice_event_ingress_not_forwarded_to_legacy():
    mapper = V2ToV1Mapper()
    voice = {
        "type": "voice_event",
        "call_family": "GROUP",
        "phase": "INGRESS",
        "direction": "RX",
        "system": "OBP-CL",
        "stream_id": 42,
        "peer_id": 73010,
        "src_id": 7060031,
        "slot": 1,
        "dst_id": 7065,
    }
    assert mapper.translate(Opcode.VOICE_EVENT_SND, json.dumps(voice).encode()) == []


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


def test_merge_ua_sessions_into_bridges_adds_on_legs():
    now = 1_700_000_000.0
    config = {
        "SYSTEM-2": {
            "MODE": "MASTER",
            "ENABLED": True,
            "PEERS": {
                (1001).to_bytes(4, "big"): {
                    "UA_SESSIONS": {"2": {"tgid": 7305, "expires_at": now + 600.0}},
                },
            },
        },
    }
    bridges = merge_ua_sessions_into_bridges({}, config, now=now)
    assert "7305" in bridges
    leg = bridges["7305"][0]
    assert leg["SYSTEM"] == "SYSTEM-2"
    assert leg["TS"] == 2
    assert leg["TGID"] == (7305).to_bytes(3, "big")
    assert int_id(leg["TGID"]) == 7305
    assert leg["TO_TYPE"] == "ON"
    assert leg["ACTIVE"] is True
    assert leg["TIMER"] == now + 600.0


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
    leg = bridges["1001:9:1"][0]
    assert leg["SYSTEM"] == "MASTER-A"
    assert leg["ACTIVE"] is True
    assert leg["TGID"] == (9).to_bytes(3, "big")
    assert int_id(leg["TGID"]) == 9


def test_routing_table_bridge_tgid_bytes_for_legacy_build_tgstats():
    mapper = V2ToV1Mapper()
    routing = {
        "type": "routing_table",
        "seq": 2,
        "routes": [
            {
                "relay_table_key": "7305",
                "legs": [
                    {
                        "system": "SYSTEM-8",
                        "ts": 2,
                        "tgid": 7305,
                        "active": True,
                        "to_type": "ON",
                        "timer_expires_at": 1_700_000_600.0,
                    },
                ],
            },
        ],
    }
    bridges = pickle.loads(
        mapper.translate(Opcode.ROUTING_TABLE_SND, json.dumps(routing).encode())[0][1:]
    )
    leg = bridges["7305"][0]
    assert isinstance(leg["TGID"], bytes)
    assert int_id(leg["TGID"]) == 7305
    assert leg["ON"] == [(7305).to_bytes(3, "big")]


def test_dashboard_state_maps_peer_options_and_static_tgs():
    mapper = V2ToV1Mapper()
    _seed_routing_masters(mapper, "MASTER-A")
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
    assert master["TS1_STATIC"] == "9,10"


def _sample_topology_eight_masters() -> dict:
    return {
        "type": "topology",
        "seq": 1,
        "systems": [
            {
                "name": f"SYSTEM-{n}",
                "mode": "MASTER",
                "enabled": True,
                "peers": [],
            }
            for n in range(1, 9)
        ],
    }


def test_dashboard_state_merges_all_topology_masters_on_hotspot_connect():
    """Legacy update_hblink_table requires every CONFIG master to exist in CTABLE."""
    mapper = V2ToV1Mapper()
    topo = _sample_topology_eight_masters()
    mapper.translate(Opcode.TOPOLOGY_SND, json.dumps(topo).encode())

    hotspot_state = {
        "type": "dashboard_state",
        "ts": 1717555200.0,
        "ctable": {
            "MASTERS": {
                "SYSTEM-8": {
                    "mode": "MASTER",
                    "peers": {
                        4197197059: {
                            "id": 4197197059,
                            "ip": "192.168.1.50",
                            "port": 62031,
                            "callsign": "HS-HOT",
                            "connection": "YES",
                        },
                    },
                },
            },
            "PEERS": {},
            "OPENBRIDGES": {},
        },
    }
    config = pickle.loads(
        mapper.translate(Opcode.STATE_SND, json.dumps(hotspot_state).encode())[0][1:]
    )
    for n in range(1, 9):
        assert f"SYSTEM-{n}" in config
        assert config[f"SYSTEM-{n}"]["MODE"] == "MASTER"
    assert config["SYSTEM-7"]["PEERS"] == {}
    peer_key = (4197197059).to_bytes(4, "big")
    assert peer_key in config["SYSTEM-8"]["PEERS"]
    assert config["SYSTEM-8"]["PEERS"][peer_key]["CONNECTION"] == "YES"


def test_dashboard_state_merge_uses_previous_config_without_topology():
    topo = _sample_topology_eight_masters()
    full = topology_to_config(topo)
    partial = dashboard_state_to_config(
        {
            "type": "dashboard_state",
            "ts": 1.0,
            "ctable": {
                "MASTERS": {
                    "SYSTEM-8": {
                        "mode": "MASTER",
                        "peers": {1001: {"id": 1001, "connection": "YES"}},
                    },
                },
                "PEERS": {},
                "OPENBRIDGES": {},
            },
        },
        previous_config=full,
        known_masters={f"SYSTEM-{n}" for n in range(1, 9)},
    )
    assert "SYSTEM-1" in partial
    assert "SYSTEM-8" in partial
    assert partial["SYSTEM-1"]["PEERS"] == {}
    assert len(partial["SYSTEM-8"]["PEERS"]) == 1


def test_routing_delta_bootstraps_without_prior_snapshot():
    mapper = V2ToV1Mapper()
    state = {
        "type": "dashboard_state",
        "ts": 1.0,
        "ctable": {
            "MASTERS": {
                "SYSTEM-8": {
                    "mode": "MASTER",
                    "peers": {1001: {"id": 1001, "connection": "YES"}},
                },
            },
            "PEERS": {},
            "OPENBRIDGES": {},
        },
    }
    mapper.translate(Opcode.STATE_SND, json.dumps(state).encode())
    delta = {
        "type": "delta",
        "since_seq": 99,
        "patch": {
            "type": "routing_table",
            "seq": 100,
            "routes": [
                {
                    "relay_table_key": "9990",
                    "legs": [
                        {"system": f"SYSTEM-{n}", "ts": 1, "tgid": 9990, "active": False, "to_type": "ON"}
                        for n in range(1, 9)
                    ],
                },
            ],
        },
    }
    frames = mapper.translate(Opcode.DELTA_SND, json.dumps(delta).encode())
    opcodes = [frame[:1] for frame in frames]
    assert Opcode.BRIDGE_SND in opcodes
    assert Opcode.CONFIG_SND in opcodes
    config = pickle.loads(next(frame for frame in frames if frame[:1] == Opcode.CONFIG_SND)[1:])
    assert "SYSTEM-1" in config
    assert "SYSTEM-8" in config


def test_inject_virtual_master_shells_on_hotspot_connect():
    state = TranslationState()
    state.inject_max_peers["SYSTEM"] = 4
    doc = {
        "type": "dashboard_state",
        "ts": 1.0,
        "ctable": {
            "MASTERS": {
                "SYSTEM-0": {
                    "mode": "MASTER",
                    "peers": {730039101: {"id": 730039101, "connection": "YES"}},
                },
            },
            "PEERS": {},
            "OPENBRIDGES": {},
        },
    }
    config = dashboard_state_to_config(doc, shell_state=state)
    for slot in range(4):
        assert f"SYSTEM-{slot}" in config
        assert config[f"SYSTEM-{slot}"]["MODE"] == "MASTER"
    assert len(config["SYSTEM-0"]["PEERS"]) == 1
    assert config["SYSTEM-1"]["PEERS"] == {}
    assert config["SYSTEM-1"]["TS1_STATIC"] is False
    assert config["SYSTEM-1"]["TS2_STATIC"] is False


def test_routing_delta_after_upstream_lost_keeps_known_masters():
    mapper = V2ToV1Mapper()
    _seed_routing_masters(mapper, *(f"SYSTEM-{n}" for n in range(1, 9)))
    mapper.on_upstream_lost()
    assert mapper._state.known_masters == {f"SYSTEM-{n}" for n in range(1, 9)}

    mapper = V2ToV1Mapper()
    _seed_routing_masters(mapper, *(f"SYSTEM-{n}" for n in range(1, 9)))
    connected = {
        "type": "dashboard_state",
        "ts": 1.0,
        "ctable": {
            "MASTERS": {
                "SYSTEM-8": {
                    "mode": "MASTER",
                    "peers": {1001: {"id": 1001, "ip": "10.0.0.2", "connection": "YES"}},
                },
            },
            "PEERS": {},
            "OPENBRIDGES": {},
        },
    }
    mapper.translate(Opcode.STATE_SND, json.dumps(connected).encode())
    disconnected = {
        "type": "dashboard_state",
        "ts": 2.0,
        "ctable": {"MASTERS": {}, "PEERS": {}, "OPENBRIDGES": {}},
    }
    config = pickle.loads(
        mapper.translate(Opcode.STATE_SND, json.dumps(disconnected).encode())[0][1:]
    )
    assert "SYSTEM-8" in config
    assert config["SYSTEM-8"]["PEERS"] == {}

