# ADN Report Proxy - v2 to v1 mapper
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

"""Pure v2 JSON → v1 pickle/CSV mapping (parity with adn-monitor report_mapper)."""

from __future__ import annotations

import json
import logging
import pickle
import time
from typing import Any

from ..domain import Opcode, TranslationError
from .translation_state import TranslationState

logger = logging.getLogger("adn-report-proxy")

_PEER_JSON_TO_LEGACY: tuple[tuple[str, str], ...] = (
    ("callsign", "CALLSIGN"),
    ("rx_freq", "RX_FREQ"),
    ("tx_freq", "TX_FREQ"),
    ("location", "LOCATION"),
    ("description", "DESCRIPTION"),
    ("url", "URL"),
    ("slots", "SLOTS"),
    ("package_id", "PACKAGE_ID"),
    ("software_id", "SOFTWARE_ID"),
    ("colorcode", "COLORCODE"),
    ("tx_power", "TX_POWER"),
)

_BYTE_PEER_FIELDS = frozenset({"RX_FREQ", "TX_FREQ", "SLOTS"})

_CALL_FAMILY_TO_CSV = {
    "GROUP": "GROUP VOICE",
    "PRIVATE": "PRIVATE VOICE",
    "UNIT": "UNIT DATA",
}


def _bytes_4(peer_id: int) -> bytes:
    return peer_id.to_bytes(4, "big")


def _legacy_peer_field(legacy_key: str, value: Any) -> Any:
    if legacy_key in _BYTE_PEER_FIELDS:
        if isinstance(value, bytes):
            return value
        text = str(value).strip()
        return text.encode("utf-8") if text else b""
    return value


def _routing_route_key(route: dict[str, Any]) -> str:
    """adn-server v2 uses ``relay_table_key``; accept legacy ``bridge_key`` too."""
    key = route.get("relay_table_key") or route.get("bridge_key")
    return str(key).strip() if key else ""


def _apply_peer_json_fields(peer: dict[str, Any], peer_conf: dict[str, Any]) -> None:
    for json_key, legacy_key in _PEER_JSON_TO_LEGACY:
        if json_key in peer:
            peer_conf[legacy_key] = _legacy_peer_field(legacy_key, peer[json_key])
    ts1 = peer.get("ts1_static")
    if isinstance(ts1, list) and ts1:
        peer_conf["TS1_STATIC"] = ",".join(str(x) for x in ts1)
    ts2 = peer.get("ts2_static")
    if isinstance(ts2, list) and ts2:
        peer_conf["TS2_STATIC"] = ",".join(str(x) for x in ts2)
    options = peer.get("options")
    if isinstance(options, str) and options.strip():
        peer_conf["OPTIONS"] = options.encode("utf-8")
    if "single_mode" in peer:
        peer_conf["SINGLE_MODE"] = bool(peer["single_mode"])
    if peer.get("ua_timer_min") is not None:
        try:
            peer_conf["UA_TIMER_MIN"] = float(peer["ua_timer_min"])
        except (TypeError, ValueError):
            pass
    if "ua_sessions" in peer:
        peer_conf["UA_SESSIONS"] = peer.get("ua_sessions") or {}


def _pickle_body(obj: Any) -> bytes:
    return pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)


def hello_v2_to_v1(payload: dict[str, Any]) -> bytes:
    """Build v1 HELLO JSON body from upstream v2 hello."""
    out = {
        "protocol": 1,
        "server": payload.get("server", "adn-server"),
        "version": payload.get("version", ""),
        "features": list(payload.get("features") or []),
    }
    systems = payload.get("systems")
    if systems is not None:
        out["systems"] = systems
    return json.dumps(out, separators=(",", ":")).encode("utf-8")


def dashboard_state_to_config(doc: dict[str, Any], *, ts: float | None = None) -> dict[str, Any]:
    """Build CONFIG dict from ``dashboard_state`` (STATE_SND / slim wire)."""
    if doc.get("type") != "dashboard_state":
        return {}
    epoch = float(doc.get("ts", time.time())) if ts is None else ts
    ctable = doc.get("ctable")
    if not isinstance(ctable, dict):
        return {}
    config: dict[str, Any] = {}

    for name, master in (ctable.get("MASTERS") or {}).items():
        if not isinstance(master, dict):
            continue
        entry: dict[str, Any] = {
            "ENABLED": True,
            "MODE": str(master.get("mode", "MASTER")),
        }
        if master.get("ip"):
            entry["IP"] = str(master["ip"])
        port = master.get("port")
        if port is not None:
            entry["PORT"] = int(port)
        if "single_mode" in master:
            entry["SINGLE_MODE"] = bool(master["single_mode"])
        if master.get("default_ua_timer") is not None:
            try:
                entry["DEFAULT_UA_TIMER"] = float(master["default_ua_timer"])
            except (TypeError, ValueError):
                pass
        peers: dict[bytes, dict[str, Any]] = {}
        raw_peers = master.get("peers") or {}
        if isinstance(raw_peers, dict):
            peer_items = raw_peers.items()
        elif isinstance(raw_peers, list):
            peer_items = ((p.get("id"), p) for p in raw_peers if isinstance(p, dict) and "id" in p)
        else:
            peer_items = ()
        for pid_key, peer in peer_items:
            if not isinstance(peer, dict):
                continue
            pid = int(peer.get("id", pid_key))
            connected_at = peer.get("connected_at")
            if connected_at is not None:
                try:
                    connected_ts = int(float(connected_at))
                except (TypeError, ValueError):
                    connected_ts = int(epoch)
            else:
                connected_ts = int(epoch)
            peer_conf: dict[str, Any] = {
                "CONNECTION": "YES",
                "CONNECTED": connected_ts,
                "IP": peer.get("ip", ""),
                "PORT": peer.get("port", ""),
                "TX_FREQ": b"",
                "RX_FREQ": b"",
                "SLOTS": b"0",
            }
            _apply_peer_json_fields(peer, peer_conf)
            peers[_bytes_4(pid)] = peer_conf
        entry["PEERS"] = peers
        config[str(name)] = entry

    for name, peer_sys in (ctable.get("PEERS") or {}).items():
        if not isinstance(peer_sys, dict):
            continue
        mode = str(peer_sys.get("mode", "PEER"))
        entry = {"ENABLED": True, "MODE": mode}
        for json_key, legacy_key in (
            ("callsign", "CALLSIGN"),
            ("location", "LOCATION"),
            ("description", "DESCRIPTION"),
            ("url", "URL"),
            ("master_ip", "MASTER_IP"),
            ("master_port", "MASTER_PORT"),
        ):
            if json_key in peer_sys and peer_sys[json_key] is not None:
                entry[legacy_key] = str(peer_sys[json_key])
        radio_id = peer_sys.get("radio_id")
        if radio_id is not None:
            entry["RADIO_ID"] = int(radio_id)
        connected_at = peer_sys.get("connected_at")
        try:
            connected_ts = int(float(connected_at)) if connected_at is not None else int(epoch)
        except (TypeError, ValueError):
            connected_ts = int(epoch)
        stats_key = "XLXSTATS" if mode == "XLXPEER" else "STATS"
        entry[stats_key] = {"CONNECTION": "YES", "CONNECTED": connected_ts}
        config[str(name)] = entry

    for name, obp in (ctable.get("OPENBRIDGES") or {}).items():
        if not isinstance(obp, dict):
            continue
        entry = {"ENABLED": True, "MODE": "OPENBRIDGE"}
        if obp.get("ip"):
            entry["IP"] = str(obp["ip"])
        port = obp.get("port")
        if port is not None:
            entry["PORT"] = int(port)
        if obp.get("network_id") is not None:
            entry["NETWORK_ID"] = _bytes_4(int(obp["network_id"]))
        if obp.get("enhanced_obp"):
            entry["ENHANCED_OBP"] = True
        config[str(name)] = entry

    return config


def topology_to_config(topology: dict[str, Any], *, ts: float | None = None) -> dict[str, Any]:
    """Build CONFIG dict from ``topology`` JSON."""
    epoch = float(topology.get("ts", time.time())) if ts is None else ts
    config: dict[str, Any] = {}
    for system in topology.get("systems", []):
        if not isinstance(system, dict):
            continue
        name = system.get("name")
        if not name:
            continue
        entry: dict[str, Any] = {
            "ENABLED": bool(system.get("enabled", True)),
            "MODE": system.get("mode", "MASTER"),
        }
        if system.get("ip"):
            entry["IP"] = str(system["ip"])
        port = system.get("port")
        if port is not None:
            entry["PORT"] = int(port)
        if "repeat" in system:
            entry["REPEAT"] = bool(system["repeat"])
        if system.get("enhanced_obp"):
            entry["ENHANCED_OBP"] = True
        if system.get("mode") == "OPENBRIDGE" and system.get("network_id") is not None:
            entry["NETWORK_ID"] = _bytes_4(int(system["network_id"]))
        peers: dict[bytes, dict[str, Any]] = {}
        for peer in system.get("peers", []):
            if not isinstance(peer, dict):
                continue
            pid = int(peer["id"])
            connected = bool(peer.get("connected", False))
            connected_at = peer.get("connected_at")
            if connected and connected_at is not None:
                try:
                    connected_ts = int(float(connected_at))
                except (TypeError, ValueError):
                    connected_ts = 0
            else:
                connected_ts = 0
            if connected and connected_ts <= 0:
                connected_ts = int(epoch)
            peer_conf: dict[str, Any] = {
                "CONNECTION": "YES" if connected else "NO",
                "CONNECTED": connected_ts if connected else 0,
                "IP": peer.get("ip", ""),
                "PORT": peer.get("port", ""),
                "TX_FREQ": b"",
                "RX_FREQ": b"",
                "SLOTS": b"0",
            }
            _apply_peer_json_fields(peer, peer_conf)
            peers[_bytes_4(pid)] = peer_conf
        entry["PEERS"] = peers
        config[str(name)] = entry
    return config


def routing_table_to_bridges(routing: dict[str, Any]) -> dict[str, Any]:
    """Build BRIDGES dict from ``routing_table`` JSON."""
    bridges: dict[str, Any] = {}
    for route in routing.get("routes", []):
        if not isinstance(route, dict):
            continue
        key = _routing_route_key(route)
        if not key:
            continue
        legs: list[dict[str, Any]] = []
        for leg in route.get("legs", []):
            if not isinstance(leg, dict):
                continue
            row: dict[str, Any] = {
                "SYSTEM": str(leg.get("system", "")),
                "TS": int(leg.get("ts", 1)),
                "TGID": int(leg.get("tgid", 0)),
                "ACTIVE": bool(leg.get("active", False)),
                "TO_TYPE": str(leg.get("to_type", "NONE")),
            }
            timer = leg.get("timer_expires_at")
            if timer is not None:
                row["TIMER"] = float(timer)
            legs.append(row)
        bridges[key] = legs
    return bridges


def voice_event_to_csv_parts(voice: dict[str, Any]) -> list[str] | None:
    """Convert ``voice_event`` to legacy BRDG_EVENT CSV field list."""
    family = voice.get("call_family")
    csv_family = _CALL_FAMILY_TO_CSV.get(family)
    if csv_family is None:
        return None
    phase = str(voice.get("phase", ""))
    if family == "UNIT" and phase == "DATA":
        csv_family = "UNIT DATA HEADER"
    direction = str(voice.get("direction", "RX"))
    parts = [
        csv_family,
        phase,
        direction,
        str(voice.get("system", "")),
        str(int(voice.get("stream_id", 0))),
        str(int(voice.get("peer_id", 0))),
        str(int(voice.get("src_id", 0))),
        str(int(voice.get("slot", 1))),
        str(int(voice.get("dst_id", 0))),
    ]
    if phase == "END":
        dur = voice.get("duration_s")
        if dur is not None:
            parts.append(f"{float(dur):.2f}")
    return parts


def merge_topology_delta(previous: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a topology delta patch into the last full snapshot."""
    merged = dict(previous)
    prev_systems = {
        s["name"]: s
        for s in previous.get("systems", [])
        if isinstance(s, dict) and s.get("name")
    }
    for system in patch.get("systems", []):
        if isinstance(system, dict) and system.get("name"):
            prev_systems[system["name"]] = system
    merged["type"] = "topology"
    merged["systems"] = list(prev_systems.values())
    if "seq" in patch:
        merged["seq"] = patch["seq"]
    if "ts" in patch:
        merged["ts"] = patch["ts"]
    return merged


def merge_routing_delta(previous: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a routing_table delta patch into the last full snapshot."""
    merged = dict(previous)
    prev_routes: dict[str, dict[str, Any]] = {}
    for r in previous.get("routes", []):
        if not isinstance(r, dict):
            continue
        key = _routing_route_key(r)
        if key:
            prev_routes[key] = r
    for route in patch.get("routes", []):
        if not isinstance(route, dict):
            continue
        key = _routing_route_key(route)
        if key:
            prev_routes[key] = route
    merged["type"] = "routing_table"
    merged["routes"] = list(prev_routes.values())
    if "seq" in patch:
        merged["seq"] = patch["seq"]
    if "ts" in patch:
        merged["ts"] = patch["ts"]
    return merged


def empty_bridges_frame() -> bytes:
    """D-25 slim wire omits routing; legacy monitors expect at least one BRIDGE_SND."""
    return Opcode.BRIDGE_SND + _pickle_body({})


def _decode_json(payload: bytes) -> dict[str, Any]:
    if not payload:
        raise TranslationError("empty JSON payload")
    obj = json.loads(payload.decode("utf-8", errors="replace"))
    if not isinstance(obj, dict):
        raise TranslationError("payload is not a JSON object")
    return obj


class V2ToV1Mapper:
    """Application service implementing :class:`V2ToV1Translator`."""

    def __init__(self, state: TranslationState | None = None) -> None:
        self._state = state if state is not None else TranslationState()

    def reset(self) -> None:
        self._state.reset()

    @property
    def upstream_is_v2(self) -> bool:
        return self._state.upstream_v2

    def translate(self, opcode: bytes, payload: bytes) -> list[bytes]:
        if not opcode:
            return []
        raw_opcode = opcode
        frame_body = payload

        # Passthrough v1 frames unchanged (upstream v1 server or mixed wire).
        if raw_opcode in (
            Opcode.CONFIG_SND,
            Opcode.BRIDGE_SND,
            Opcode.BRDG_EVENT,
            Opcode.LINK_EVENT,
        ):
            self._state.upstream_v2 = False
            return [raw_opcode + frame_body]

        if raw_opcode == Opcode.HELLO:
            try:
                doc = _decode_json(frame_body)
            except (json.JSONDecodeError, TranslationError):
                return [Opcode.HELLO + frame_body]
            if doc.get("report_protocol") == 2:
                self._state.upstream_v2 = True
                out = [Opcode.HELLO + hello_v2_to_v1(doc)]
                if not self._state.bridges_frame_sent:
                    out.append(empty_bridges_frame())
                    self._state.bridges_frame_sent = True
                return out
            return [Opcode.HELLO + frame_body]

        try:
            doc = _decode_json(frame_body)
        except json.JSONDecodeError as e:
            logger.debug("skip non-JSON opcode %s: %s", raw_opcode.hex(), e)
            return []

        msg_type = doc.get("type", "")

        self._state.upstream_v2 = True

        if raw_opcode == Opcode.STATE_SND or msg_type == "dashboard_state":
            config = dashboard_state_to_config(doc)
            if not config:
                return []
            return [Opcode.CONFIG_SND + _pickle_body(config)]

        if raw_opcode == Opcode.TOPOLOGY_SND or msg_type == "topology":
            self._state.topology_snapshot = doc
            self._state.topology_seq = int(doc.get("seq", self._state.topology_seq))
            config = topology_to_config(doc)
            if not config:
                return []
            return [Opcode.CONFIG_SND + _pickle_body(config)]

        if raw_opcode == Opcode.ROUTING_TABLE_SND or msg_type == "routing_table":
            self._state.routing_snapshot = doc
            self._state.routing_seq = int(doc.get("seq", self._state.routing_seq))
            bridges = routing_table_to_bridges(doc)
            self._state.bridges_frame_sent = True
            return [Opcode.BRIDGE_SND + _pickle_body(bridges)]

        if raw_opcode == Opcode.VOICE_EVENT_SND or msg_type == "voice_event":
            parts = voice_event_to_csv_parts(doc)
            if not parts:
                return []
            return [Opcode.BRDG_EVENT + ",".join(parts).encode("utf-8")]

        if raw_opcode == Opcode.DELTA_SND or msg_type == "delta":
            return self._translate_delta(doc)

        return []

    def _translate_delta(self, delta: dict[str, Any]) -> list[bytes]:
        patch = delta.get("patch")
        if not isinstance(patch, dict):
            return []
        patch_type = patch.get("type")
        since_seq = delta.get("since_seq")
        if patch_type == "topology":
            if self._state.topology_snapshot is None:
                logger.debug("DELTA_SND topology patch without prior snapshot")
                return []
            if since_seq is not None and int(since_seq) != self._state.topology_seq:
                logger.debug(
                    "DELTA_SND topology since_seq=%s expected %s",
                    since_seq,
                    self._state.topology_seq,
                )
            merged = merge_topology_delta(self._state.topology_snapshot, patch)
            self._state.topology_snapshot = merged
            self._state.topology_seq = int(merged.get("seq", self._state.topology_seq))
            config = topology_to_config(merged)
            if not config:
                return []
            return [Opcode.CONFIG_SND + _pickle_body(config)]
        if patch_type == "routing_table":
            if self._state.routing_snapshot is None:
                logger.debug("DELTA_SND routing patch without prior snapshot")
                return []
            if since_seq is not None and int(since_seq) != self._state.routing_seq:
                logger.debug(
                    "DELTA_SND routing since_seq=%s expected %s",
                    since_seq,
                    self._state.routing_seq,
                )
            merged = merge_routing_delta(self._state.routing_snapshot, patch)
            self._state.routing_snapshot = merged
            self._state.routing_seq = int(merged.get("seq", self._state.routing_seq))
            bridges = routing_table_to_bridges(merged)
            self._state.bridges_frame_sent = True
            return [Opcode.BRIDGE_SND + _pickle_body(bridges)]
        logger.debug("DELTA_SND unknown patch type=%s", patch_type)
        return []
