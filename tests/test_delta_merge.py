# ADN Report Proxy - delta merge tests
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

"""Tests for DELTA_SND merge → v1 snapshots."""

from __future__ import annotations

import json
import pickle

from adn_report_proxy.application.v2_to_v1_mapper import V2ToV1Mapper
from adn_report_proxy.domain import Opcode


def test_topology_delta_emits_config_snd():
    mapper = V2ToV1Mapper()
    full = {
        "type": "topology",
        "seq": 1,
        "ts": 100.0,
        "systems": [{"name": "MASTER-A", "mode": "MASTER", "enabled": True, "peers": []}],
    }
    mapper.translate(Opcode.TOPOLOGY_SND, json.dumps(full).encode())

    delta = {
        "type": "delta",
        "since_seq": 1,
        "patch": {
            "type": "topology",
            "seq": 2,
            "systems": [{"name": "MASTER-B", "mode": "MASTER", "enabled": True, "peers": []}],
        },
    }
    frames = mapper.translate(Opcode.DELTA_SND, json.dumps(delta).encode())
    assert len(frames) == 1
    assert frames[0][:1] == Opcode.CONFIG_SND
    config = pickle.loads(frames[0][1:])
    assert "MASTER-A" in config
    assert "MASTER-B" in config


def test_routing_delta_without_prior_snapshot_emits_bridge_snd():
    mapper = V2ToV1Mapper()
    delta = {
        "type": "delta",
        "since_seq": 5,
        "patch": {
            "type": "routing_table",
            "seq": 6,
            "routes": [
                {
                    "relay_table_key": "43:9:1",
                    "legs": [{"system": "M2", "ts": 1, "tgid": 9, "active": False, "to_type": "OFF"}],
                },
            ],
        },
    }
    frames = mapper.translate(Opcode.DELTA_SND, json.dumps(delta).encode())
    assert len(frames) == 1
    assert frames[0][:1] == Opcode.BRIDGE_SND
    bridges = pickle.loads(frames[0][1:])
    assert "43:9:1" in bridges


def test_routing_delta_emits_bridge_snd_with_relay_table_key():
    mapper = V2ToV1Mapper()
    full = {
        "type": "routing_table",
        "seq": 1,
        "routes": [
            {
                "relay_table_key": "42:9:1",
                "legs": [{"system": "M1", "ts": 1, "tgid": 9, "active": True, "to_type": "ON"}],
            },
        ],
    }
    mapper.translate(Opcode.ROUTING_TABLE_SND, json.dumps(full).encode())

    delta = {
        "type": "delta",
        "since_seq": 1,
        "patch": {
            "type": "routing_table",
            "seq": 2,
            "routes": [
                {
                    "relay_table_key": "43:9:1",
                    "legs": [{"system": "M2", "ts": 1, "tgid": 9, "active": False, "to_type": "OFF"}],
                },
            ],
        },
    }
    frames = mapper.translate(Opcode.DELTA_SND, json.dumps(delta).encode())
    assert len(frames) == 1
    assert frames[0][:1] == Opcode.BRIDGE_SND
    bridges = pickle.loads(frames[0][1:])
    assert "42:9:1" in bridges
    assert "43:9:1" in bridges
