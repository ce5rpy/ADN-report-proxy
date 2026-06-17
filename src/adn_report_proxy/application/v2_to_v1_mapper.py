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

# Legacy inject-only proxy fans hotspots into ``{target}-0..N-1`` virtual masters.
_DEFAULT_INJECT_MAX_PEERS = 200

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

# Legacy dashboard.py reads these keys unconditionally (CONFIG_SND pickle parity).
_LEGACY_HOTSPOT_PEER_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("PACKAGE_ID", b""),
    ("SOFTWARE_ID", b""),
    ("LOCATION", b""),
    ("DESCRIPTION", b""),
    ("URL", b""),
    ("CALLSIGN", b""),
    ("COLORCODE", b""),
    ("TX_POWER", b""),
    ("LATITUDE", b"0.000000"),
    ("LONGITUDE", b"0.000000"),
    ("HEIGHT", b""),
)

_LEGACY_COORD_DEFAULT = b"0.000000"

_CALL_FAMILY_TO_CSV = {
    "GROUP": "GROUP VOICE",
    "PRIVATE": "PRIVATE VOICE",
    "UNIT": "UNIT DATA",
}


def _bytes_4(peer_id: int) -> bytes:
    return peer_id.to_bytes(4, "big")


def _bytes_3(value: int) -> bytes:
    return int(value).to_bytes(3, "big")


def _legacy_tgid_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    return _bytes_3(int(value))


def _finalize_legacy_bridge_leg(leg: dict[str, Any]) -> dict[str, Any]:
    """Legacy ``BRIDGE_SND`` pickle parity (``int_id(system['TGID'])`` expects bytes)."""
    row = dict(leg)
    tgid_b = _legacy_tgid_bytes(row.get("TGID", 0))
    row["TGID"] = tgid_b
    on = row.get("ON")
    if not on:
        row["ON"] = [tgid_b]
    else:
        row["ON"] = [_legacy_tgid_bytes(x) for x in on]
    off = row.get("OFF")
    row["OFF"] = [] if off is None else [_legacy_tgid_bytes(x) for x in off]
    row.setdefault("RESET", [])
    row.setdefault("TIMEOUT", 600.0)
    return row


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


def _apply_legacy_master_fields(entry: dict[str, Any], source: dict[str, Any]) -> None:
    """``build_hblink_table`` requires ``REPEAT`` on every enabled MASTER."""
    if entry.get("MODE") != "MASTER":
        return
    entry["REPEAT"] = bool(source.get("repeat", source.get("REPEAT", False)))


def _parse_options_static_tgs(options: Any) -> tuple[list[str], list[str]]:
    """Parse RPTO ``TS1=`` / ``TS2=`` fields from dashboard_state peer options."""
    if options is None:
        return [], []
    if isinstance(options, bytes):
        text = options.decode("utf-8", errors="replace")
    else:
        text = str(options)
    text = text.rstrip("\x00").strip()
    if not text:
        return [], []
    parsed: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        parsed[key.strip().upper()] = value.strip()
    for old, new in (("TS1", "TS1_STATIC"), ("TS2", "TS2_STATIC")):
        if old in parsed and new not in parsed:
            parsed[new] = parsed[old]
    ts1_parts: list[str] = []
    if "TS1_1" in parsed:
        ts1_parts.append(parsed["TS1_1"])
        for idx in range(2, 10):
            piece = parsed.get(f"TS1_{idx}")
            if piece:
                ts1_parts.append(piece)
    elif parsed.get("TS1_STATIC"):
        ts1_parts = [x.strip() for x in parsed["TS1_STATIC"].split(",") if x.strip()]
    ts2_parts: list[str] = []
    if "TS2_1" in parsed:
        ts2_parts.append(parsed["TS2_1"])
        for idx in range(2, 10):
            piece = parsed.get(f"TS2_{idx}")
            if piece:
                ts2_parts.append(piece)
    elif parsed.get("TS2_STATIC"):
        ts2_parts = [x.strip() for x in parsed["TS2_STATIC"].split(",") if x.strip()]
    return ts1_parts, ts2_parts


def _topology_master_lookup(topology: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(topology, dict):
        return {}
    return {
        str(row["name"]): row
        for row in topology.get("systems", [])
        if isinstance(row, dict) and row.get("name")
    }


def _static_tg_list(value: Any) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _merge_static_tg_lists(*sources: Any) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for source in sources:
        for item in _static_tg_list(source):
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def _legacy_master_static_tg_value(items: list[str]) -> str | bool:
    """Legacy CONFIG uses comma strings; empty means ``False`` (dashboard → ``[]``)."""
    if not items:
        return False
    return ",".join(items)


def _apply_legacy_master_static_tgs(
    entry: dict[str, Any],
    source: dict[str, Any],
    *,
    peers: list[dict[str, Any]] | None = None,
) -> None:
    """``build_tgstats`` reads ``CONFIG[master]['TS1_STATIC']`` / ``TS2_STATIC``."""
    if entry.get("MODE") != "MASTER":
        return
    peer_ts1: list[str] = []
    peer_ts2: list[str] = []
    for peer in peers or ():
        if not isinstance(peer, dict):
            continue
        peer_ts1.extend(_static_tg_list(peer.get("ts1_static")))
        peer_ts2.extend(_static_tg_list(peer.get("ts2_static")))
        peer_ts1.extend(_static_tg_list(peer.get("TS1_STATIC")))
        peer_ts2.extend(_static_tg_list(peer.get("TS2_STATIC")))
        opt_ts1, opt_ts2 = _parse_options_static_tgs(peer.get("options"))
        peer_ts1.extend(opt_ts1)
        peer_ts2.extend(opt_ts2)
    ts1 = _merge_static_tg_lists(
        source.get("ts1_static"),
        source.get("TS1_STATIC"),
        peer_ts1,
    )
    ts2 = _merge_static_tg_lists(
        source.get("ts2_static"),
        source.get("TS2_STATIC"),
        peer_ts2,
    )
    entry["TS1_STATIC"] = _legacy_master_static_tg_value(ts1)
    entry["TS2_STATIC"] = _legacy_master_static_tg_value(ts2)


def _ensure_legacy_coord(peer_conf: dict[str, Any], key: str) -> None:
    """Legacy dash_db.py calls ``float(LATITUDE)``; empty coords must be numeric."""
    raw = peer_conf.get(key, b"")
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="ignore").strip().lstrip("+")
    else:
        text = str(raw).strip().lstrip("+")
    if not text:
        peer_conf[key] = _LEGACY_COORD_DEFAULT
    elif not isinstance(raw, bytes):
        peer_conf[key] = text.encode("utf-8")


def _finalize_legacy_hotspot_peer(peer_conf: dict[str, Any]) -> None:
    """Fill CONFIG peer rows for legacy ``add_hb_peer`` (legacy dashboard.py)."""
    peer_conf.setdefault("TX_FREQ", b"")
    peer_conf.setdefault("RX_FREQ", b"")
    peer_conf.setdefault("SLOTS", b"0")
    peer_conf.setdefault("IP", "")
    peer_conf.setdefault("PORT", "")
    peer_conf.setdefault("CONNECTION", "NO")
    peer_conf.setdefault("CONNECTED", 0)
    for key, default in _LEGACY_HOTSPOT_PEER_DEFAULTS:
        peer_conf.setdefault(key, default)
    _ensure_legacy_coord(peer_conf, "LATITUDE")
    _ensure_legacy_coord(peer_conf, "LONGITUDE")


def _finalize_legacy_upstream_peer(
    entry: dict[str, Any],
    *,
    mode: str,
    connected_ts: int,
) -> None:
    """Fill standalone PEER / XLXPEER rows for legacy ``build_hblink_table``."""
    for key in ("CALLSIGN", "LOCATION", "DESCRIPTION", "URL", "MASTER_IP", "MASTER_PORT"):
        entry.setdefault(key, "")
    entry.setdefault("RADIO_ID", 0)
    entry.setdefault("SLOTS", b"0")
    stats_key = "XLXSTATS" if mode == "XLXPEER" else "STATS"
    stats = entry.setdefault(
        stats_key,
        {"CONNECTION": "YES", "CONNECTED": connected_ts, "PINGS_SENT": 0, "PINGS_ACKD": 0},
    )
    stats.setdefault("CONNECTION", "YES")
    stats.setdefault("CONNECTED", connected_ts)
    stats.setdefault("PINGS_SENT", 0)
    stats.setdefault("PINGS_ACKD", 0)


def _finalize_legacy_openbridge(entry: dict[str, Any], source: dict[str, Any]) -> None:
    """Legacy dashboard expects ``TARGET_IP`` / ``TARGET_PORT`` on OPENBRIDGE systems."""
    ip = source.get("target_ip") or source.get("ip") or entry.get("IP") or ""
    port = source.get("target_port")
    if port is None:
        port = source.get("port")
    if port is None:
        port = entry.get("PORT", 0)
    entry.setdefault("TARGET_IP", str(ip))
    try:
        entry.setdefault("TARGET_PORT", int(port))
    except (TypeError, ValueError):
        entry.setdefault("TARGET_PORT", 0)


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


def _routing_master_names(routing: dict[str, Any]) -> set[str]:
    """Masters implied by v2 ``routing_table`` legs (echo seed lists every MASTER)."""
    names: set[str] = set()
    for route in routing.get("routes", []):
        if not isinstance(route, dict):
            continue
        for leg in route.get("legs", []):
            if not isinstance(leg, dict):
                continue
            name = str(leg.get("system", "")).strip()
            if not name or name.startswith("OBP") or name == "ECHO":
                continue
            names.add(name)
    return names


def _parse_virtual_inject_master(name: str) -> tuple[str, int] | None:
    if name.startswith("OBP") or name in ("ECHO", "D-APRS"):
        return None
    if "-" not in name:
        return None
    base, _, slot_s = name.rpartition("-")
    if not base or not slot_s.isdigit():
        return None
    return base, int(slot_s)


def _master_names_from_config(config: dict[str, Any] | None) -> set[str]:
    if not isinstance(config, dict):
        return set()
    return {
        str(name)
        for name, entry in config.items()
        if isinstance(entry, dict) and entry.get("MODE") == "MASTER"
    }


def _note_inject_master(state: TranslationState | None, name: str) -> None:
    if state is None:
        return
    parsed = _parse_virtual_inject_master(name)
    if parsed is None:
        return
    base, _slot = parsed
    state.inject_bases.add(base)


def _note_inject_routing_target(state: TranslationState | None, system: str) -> None:
    """Physical inject target on HBP wire (``SYSTEM``) fans out to ``SYSTEM-N`` masters."""
    if state is None or not system or system.startswith("OBP") or system == "ECHO":
        return
    if "-" in system:
        _note_inject_master(state, system)
        return
    state.inject_bases.add(system)


def _collect_master_shell_names(
    partial: dict[str, Any],
    merged: dict[str, Any],
    known_masters: set[str] | None,
    previous_config: dict[str, Any] | None,
    state: TranslationState | None,
) -> set[str]:
    names: set[str] = set(known_masters or [])
    names.update(_master_names_from_config(partial))
    names.update(_master_names_from_config(merged))
    names.update(_master_names_from_config(previous_config))
    for name in list(names):
        _note_inject_master(state, name)
    expanded = set(names)
    if state is not None:
        for base in state.inject_bases:
            max_peers = state.inject_max_peers.get(base, _DEFAULT_INJECT_MAX_PEERS)
            for slot in range(max_peers):
                expanded.add(f"{base}-{slot}")
    return expanded


def _topology_master_names(topology: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for system in topology.get("systems", []):
        if not isinstance(system, dict):
            continue
        name = system.get("name")
        if not name:
            continue
        if str(system.get("mode", "MASTER")) == "MASTER":
            names.add(str(name))
    return names


def _empty_master_shell(template: dict[str, Any] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ENABLED": True,
        "MODE": "MASTER",
        "REPEAT": False,
        "PEERS": {},
        "TS1_STATIC": False,
        "TS2_STATIC": False,
    }
    if template:
        for key in (
            "IP",
            "PORT",
            "SINGLE_MODE",
            "DEFAULT_UA_TIMER",
            "TS1_STATIC",
            "TS2_STATIC",
            "REPEAT",
        ):
            if key in template:
                entry[key] = template[key]
    return entry


def _ensure_known_master_shells(
    config: dict[str, Any],
    known_masters: set[str] | None,
) -> dict[str, Any]:
    """Legacy ``update_hblink_table`` requires every CONFIG master to exist in CTABLE."""
    if not known_masters:
        return config
    for name in sorted(known_masters):
        existing = config.get(name)
        if not isinstance(existing, dict):
            config[name] = _empty_master_shell(
                existing if isinstance(existing, dict) else None
            )
            continue
        if existing.get("MODE") != "MASTER":
            continue
        existing.setdefault("ENABLED", True)
        existing.setdefault("REPEAT", False)
        existing.setdefault("PEERS", {})
        existing.setdefault("TS1_STATIC", False)
        existing.setdefault("TS2_STATIC", False)
    return config


def dashboard_state_to_config(
    doc: dict[str, Any],
    *,
    ts: float | None = None,
    topology: dict[str, Any] | None = None,
    previous_config: dict[str, Any] | None = None,
    known_masters: set[str] | None = None,
    shell_state: TranslationState | None = None,
) -> dict[str, Any]:
    """Build CONFIG dict from ``dashboard_state`` (STATE_SND / slim wire)."""
    if doc.get("type") != "dashboard_state":
        return {}
    epoch = float(doc.get("ts", time.time())) if ts is None else ts
    ctable = doc.get("ctable")
    if not isinstance(ctable, dict):
        return {}
    config: dict[str, Any] = {}
    topo_masters = _topology_master_lookup(topology)

    for name, master in (ctable.get("MASTERS") or {}).items():
        _note_inject_master(shell_state, str(name))
        if not isinstance(master, dict):
            continue
        static_source = dict(master)
        topo_row = topo_masters.get(str(name))
        if isinstance(topo_row, dict):
            for key in ("ts1_static", "ts2_static", "repeat"):
                if key in topo_row:
                    static_source[key] = topo_row[key]
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
        raw_peer_rows: list[dict[str, Any]] = []
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
            _finalize_legacy_hotspot_peer(peer_conf)
            peers[_bytes_4(pid)] = peer_conf
            raw_peer_rows.append(peer)
        entry["PEERS"] = peers
        _apply_legacy_master_fields(entry, static_source)
        _apply_legacy_master_static_tgs(entry, static_source, peers=raw_peer_rows)
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
        _finalize_legacy_upstream_peer(entry, mode=mode, connected_ts=connected_ts)
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
        _finalize_legacy_openbridge(entry, obp)
        config[str(name)] = entry

    return _complete_legacy_config(
        config,
        topology=topology,
        previous_config=previous_config,
        known_masters=known_masters,
        shell_state=shell_state,
        ts=epoch,
    )


_MASTER_OVERLAY_KEYS = (
    "SINGLE_MODE",
    "DEFAULT_UA_TIMER",
    "IP",
    "PORT",
    "REPEAT",
    "TS1_STATIC",
    "TS2_STATIC",
)


def _overlay_master_entry(base: dict[str, Any], entry: dict[str, Any]) -> None:
    base["PEERS"] = entry.get("PEERS", {})
    for key in _MASTER_OVERLAY_KEYS:
        if key in entry:
            base[key] = entry[key]


def _merge_dashboard_state_into_topology_config(
    partial: dict[str, Any],
    topology: dict[str, Any],
    *,
    ts: float | None = None,
) -> dict[str, Any]:
    """Legacy ``update_hblink_table`` needs every enabled MASTER present in CONFIG/CTABLE."""
    full = topology_to_config(topology, ts=ts)
    for name, entry in partial.items():
        if name not in full:
            full[name] = entry
            continue
        base = full[name]
        if entry.get("MODE") == "MASTER" and base.get("MODE") == "MASTER":
            _overlay_master_entry(base, entry)
        else:
            full[name] = entry
    return full


def _merge_dashboard_state_into_previous_config(
    partial: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    full: dict[str, Any] = {}
    for name, entry in previous.items():
        if isinstance(entry, dict):
            full[name] = dict(entry)
            if entry.get("MODE") == "MASTER":
                full[name]["PEERS"] = dict(entry.get("PEERS") or {})
        else:
            full[name] = entry
    for name, entry in partial.items():
        if name not in full:
            full[name] = entry
            continue
        base = full[name]
        if entry.get("MODE") == "MASTER" and isinstance(base, dict) and base.get("MODE") == "MASTER":
            _overlay_master_entry(base, entry)
        else:
            full[name] = entry
    for name, entry in full.items():
        if isinstance(entry, dict) and entry.get("MODE") == "MASTER" and name not in partial:
            entry["PEERS"] = {}
    return full


def _complete_legacy_config(
    partial: dict[str, Any],
    *,
    topology: dict[str, Any] | None,
    previous_config: dict[str, Any] | None,
    known_masters: set[str] | None,
    shell_state: TranslationState | None,
    ts: float | None,
) -> dict[str, Any]:
    if topology:
        merged = _merge_dashboard_state_into_topology_config(partial, topology, ts=ts)
    elif previous_config:
        merged = _merge_dashboard_state_into_previous_config(partial, previous_config)
    else:
        merged = partial
    shell_names = _collect_master_shell_names(
        partial, merged, known_masters, previous_config, shell_state
    )
    return _ensure_known_master_shells(merged, shell_names)


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
        _apply_legacy_master_fields(entry, system)
        if system.get("enhanced_obp"):
            entry["ENHANCED_OBP"] = True
        if system.get("mode") == "OPENBRIDGE" and system.get("network_id") is not None:
            entry["NETWORK_ID"] = _bytes_4(int(system["network_id"]))
        peers: dict[bytes, dict[str, Any]] = {}
        raw_peer_rows: list[dict[str, Any]] = []
        for peer in system.get("peers", []):
            if not isinstance(peer, dict):
                continue
            raw_peer_rows.append(peer)
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
            _finalize_legacy_hotspot_peer(peer_conf)
            peers[_bytes_4(pid)] = peer_conf
        entry["PEERS"] = peers
        _apply_legacy_master_static_tgs(entry, system, peers=raw_peer_rows)
        if entry.get("MODE") == "OPENBRIDGE":
            _finalize_legacy_openbridge(entry, system)
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
                "TO_TYPE": str(leg.get("to_type", "NONE")).upper(),
            }
            timer = leg.get("timer_expires_at")
            if timer is not None:
                row["TIMER"] = float(timer)
            legs.append(_finalize_legacy_bridge_leg(row))
        bridges[key] = legs
    return bridges


def merge_ua_sessions_into_bridges(
    bridges: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Legacy ``build_tgstats`` reads BRIDGE ``TO_TYPE=ON`` legs, not ``UA_SESSIONS``.

    adn-server v2 publishes per-peer ``ua_sessions`` in ``dashboard_state``; merge them
    into the BRIDGES snapshot so legacy ``SINGLE_TS*`` / timeout columns update.
    """
    if not config:
        return bridges
    epoch = time.time() if now is None else now
    merged: dict[str, list[dict[str, Any]]] = {
        key: [_finalize_legacy_bridge_leg(dict(leg)) for leg in legs]
        for key, legs in bridges.items()
    }
    for sys_name, sys_cfg in config.items():
        if not isinstance(sys_cfg, dict) or sys_cfg.get("MODE") != "MASTER":
            continue
        for peer_cfg in sys_cfg.get("PEERS", {}).values():
            if not isinstance(peer_cfg, dict):
                continue
            ua = peer_cfg.get("UA_SESSIONS")
            if not isinstance(ua, dict):
                continue
            for slot_key, sess in ua.items():
                if not isinstance(sess, dict):
                    continue
                try:
                    slot = int(slot_key)
                    tgid = int(sess.get("tgid", 0) or 0)
                    expires = float(sess.get("expires_at", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if tgid <= 0 or expires <= epoch or slot not in (1, 2):
                    continue
                table_key = str(tgid)
                legs = merged.setdefault(table_key, [])
                updated = False
                for leg in legs:
                    if leg.get("SYSTEM") == sys_name and int(leg.get("TS", 0)) == slot:
                        leg["TGID"] = _legacy_tgid_bytes(tgid)
                        leg["ACTIVE"] = True
                        leg["TO_TYPE"] = "ON"
                        leg["TIMER"] = expires
                        if not leg.get("ON"):
                            leg["ON"] = [leg["TGID"]]
                        updated = True
                        break
                if not updated:
                    legs.append(
                        _finalize_legacy_bridge_leg(
                            {
                                "SYSTEM": sys_name,
                                "TS": slot,
                                "TGID": tgid,
                                "ACTIVE": True,
                                "TO_TYPE": "ON",
                                "TIMER": expires,
                            }
                        )
                    )
    return merged


def voice_event_to_csv_parts(voice: dict[str, Any]) -> list[str] | None:
    """Convert ``voice_event`` to legacy BRDG_EVENT CSV field list."""
    family = voice.get("call_family")
    csv_family = _CALL_FAMILY_TO_CSV.get(family)
    if csv_family is None:
        return None
    phase = str(voice.get("phase", "")).upper()
    if phase == "INGRESS":
        # v2 OpenBridge debug leg; legacy adn-dmr-server / legacy dashboard log only knows START/END.
        return None
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

    def on_upstream_lost(self) -> None:
        self._state.on_upstream_lost()

    @property
    def upstream_is_v2(self) -> bool:
        return self._state.upstream_v2

    def _legacy_bridges_from_routing(self, routing: dict[str, Any]) -> dict[str, Any]:
        bridges = routing_table_to_bridges(routing)
        return merge_ua_sessions_into_bridges(bridges, self._state.last_config)

    def _ingest_known_masters(self, routing: dict[str, Any]) -> None:
        for route in routing.get("routes", []):
            if not isinstance(route, dict):
                continue
            for leg in route.get("legs", []):
                if isinstance(leg, dict):
                    _note_inject_routing_target(self._state, str(leg.get("system", "")).strip())
        self._state.known_masters.update(_routing_master_names(routing))

    def _track_config_masters(self, config: dict[str, Any]) -> None:
        for name, entry in config.items():
            if isinstance(entry, dict) and entry.get("MODE") == "MASTER":
                self._state.known_masters.add(str(name))

    def _apply_routing_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        if self._state.routing_snapshot is None:
            merged = {
                "type": "routing_table",
                "seq": int(patch.get("seq", 0)),
                "ts": float(patch.get("ts", time.time())),
                "routes": list(patch.get("routes") or []),
            }
        else:
            merged = merge_routing_delta(self._state.routing_snapshot, patch)
        self._state.routing_snapshot = merged
        self._state.routing_seq = int(merged.get("seq", self._state.routing_seq))
        self._ingest_known_masters(merged)
        return merged

    def _frames_from_routing(self, routing: dict[str, Any]) -> list[bytes]:
        bridges = self._legacy_bridges_from_routing(routing)
        self._state.bridges_frame_sent = True
        out = [Opcode.BRIDGE_SND + _pickle_body(bridges)]
        config_frame = self._maybe_config_frame_after_routing()
        if config_frame is not None:
            out.append(config_frame)
        return out

    def _ingest_topology_masters(self, topology: dict[str, Any]) -> None:
        self._state.known_masters.update(_topology_master_names(topology))

    def _finalize_config_shells(self, config: dict[str, Any]) -> dict[str, Any]:
        shell_names = _collect_master_shell_names(
            config,
            config,
            self._state.known_masters,
            self._state.last_config,
            self._state,
        )
        return _ensure_known_master_shells(config, shell_names)

    def _config_from_dashboard_state(self, doc: dict[str, Any]) -> dict[str, Any]:
        return dashboard_state_to_config(
            doc,
            topology=self._state.topology_snapshot,
            previous_config=self._state.last_config,
            known_masters=self._state.known_masters,
            shell_state=self._state,
        )

    def _config_frame_from_dashboard_state(self, doc: dict[str, Any]) -> bytes | None:
        config = self._config_from_dashboard_state(doc)
        if not config:
            return None
        self._track_config_masters(config)
        self._state.last_config = config
        return Opcode.CONFIG_SND + _pickle_body(config)

    def _maybe_config_frame_after_routing(self) -> bytes | None:
        doc = self._state.last_dashboard_state
        if doc is None:
            return None
        return self._config_frame_from_dashboard_state(doc)

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
            self._state.last_dashboard_state = doc
            frame = self._config_frame_from_dashboard_state(doc)
            if frame is None:
                return []
            return [frame]

        if raw_opcode == Opcode.TOPOLOGY_SND or msg_type == "topology":
            self._state.topology_snapshot = doc
            self._state.topology_seq = int(doc.get("seq", self._state.topology_seq))
            self._ingest_topology_masters(doc)
            config = self._finalize_config_shells(topology_to_config(doc))
            if not config:
                return []
            self._track_config_masters(config)
            self._state.last_config = config
            return [Opcode.CONFIG_SND + _pickle_body(config)]

        if raw_opcode == Opcode.ROUTING_TABLE_SND or msg_type == "routing_table":
            self._state.routing_snapshot = doc
            self._state.routing_seq = int(doc.get("seq", self._state.routing_seq))
            self._ingest_known_masters(doc)
            return self._frames_from_routing(doc)

        if raw_opcode == Opcode.VOICE_EVENT_SND or msg_type == "voice_event":
            _note_inject_master(self._state, str(doc.get("system", "")))
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
                merged = {
                    "type": "topology",
                    "seq": int(patch.get("seq", 0)),
                    "ts": float(patch.get("ts", time.time())),
                    "systems": list(patch.get("systems") or []),
                }
            else:
                if since_seq is not None and int(since_seq) != self._state.topology_seq:
                    logger.debug(
                        "DELTA_SND topology since_seq=%s expected %s",
                        since_seq,
                        self._state.topology_seq,
                    )
                merged = merge_topology_delta(self._state.topology_snapshot, patch)
            self._state.topology_snapshot = merged
            self._state.topology_seq = int(merged.get("seq", self._state.topology_seq))
            self._ingest_topology_masters(merged)
            config = self._finalize_config_shells(topology_to_config(merged))
            if not config:
                return []
            self._track_config_masters(config)
            self._state.last_config = config
            return [Opcode.CONFIG_SND + _pickle_body(config)]
        if patch_type == "routing_table":
            if (
                since_seq is not None
                and self._state.routing_snapshot is not None
                and int(since_seq) != self._state.routing_seq
            ):
                logger.debug(
                    "DELTA_SND routing since_seq=%s expected %s",
                    since_seq,
                    self._state.routing_seq,
                )
            merged = self._apply_routing_patch(patch)
            return self._frames_from_routing(merged)
        logger.debug("DELTA_SND unknown patch type=%s", patch_type)
        return []
