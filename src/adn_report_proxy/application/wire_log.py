# ADN Report Proxy - wire protocol logging helpers
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

"""Human-readable summaries for report wire traffic (debugging legacy proxy)."""

from __future__ import annotations

import json
import pickle
from typing import Any

from ..domain import Opcode

_OPCODE_NAMES: dict[bytes, str] = {
    Opcode.CONFIG_REQ: "CONFIG_REQ",
    Opcode.CONFIG_SND: "CONFIG_SND",
    Opcode.BRIDGE_REQ: "BRIDGE_REQ",
    Opcode.BRIDGE_SND: "BRIDGE_SND",
    Opcode.LINK_EVENT: "LINK_EVENT",
    Opcode.BRDG_EVENT: "BRDG_EVENT",
    Opcode.TOPOLOGY_SND: "TOPOLOGY_SND",
    Opcode.ROUTING_TABLE_SND: "ROUTING_TABLE_SND",
    Opcode.VOICE_EVENT_SND: "VOICE_EVENT_SND",
    Opcode.DELTA_SND: "DELTA_SND",
    Opcode.STATE_SND: "STATE_SND",
    Opcode.STATE_REQ: "STATE_REQ",
    Opcode.HELLO: "HELLO",
}


def opcode_name(opcode: bytes) -> str:
    if not opcode:
        return "EMPTY"
    return _OPCODE_NAMES.get(opcode, f"0x{opcode.hex()}")


def _json_type_summary(doc: dict[str, Any]) -> str:
    msg_type = str(doc.get("type", "?"))
    parts = [msg_type]
    if "seq" in doc:
        parts.append(f"seq={doc['seq']}")
    if msg_type == "dashboard_state":
        ctable = doc.get("ctable") if isinstance(doc.get("ctable"), dict) else {}
        masters = ctable.get("MASTERS") if isinstance(ctable, dict) else {}
        peer_count = 0
        if isinstance(masters, dict):
            for master in masters.values():
                if isinstance(master, dict):
                    peer_count += len(master.get("peers") or {})
        parts.append(f"masters={len(masters or {})} peers={peer_count}")
    elif msg_type == "routing_table":
        routes = doc.get("routes")
        parts.append(f"routes={len(routes) if isinstance(routes, list) else 0}")
    elif msg_type == "topology":
        systems = doc.get("systems")
        parts.append(f"systems={len(systems) if isinstance(systems, list) else 0}")
    elif msg_type == "delta":
        patch = doc.get("patch") if isinstance(doc.get("patch"), dict) else {}
        parts.append(f"patch={patch.get('type', '?')}")
        if doc.get("since_seq") is not None:
            parts.append(f"since_seq={doc.get('since_seq')}")
    elif msg_type == "voice_event":
        parts.append(
            f"{doc.get('call_family', '?')} {doc.get('phase', '?')} {doc.get('system', '?')}"
        )
    elif msg_type == "hello":
        parts.append(f"protocol={doc.get('report_protocol', '?')}")
    return " ".join(parts)


def _pickle_summary(payload: bytes) -> str:
    try:
        obj = pickle.loads(payload)
    except Exception as exc:
        return f"pickle {len(payload)} B (unreadable: {exc})"
    if not isinstance(obj, dict):
        return f"pickle {len(payload)} B"
    masters = sum(
        1 for entry in obj.values() if isinstance(entry, dict) and entry.get("MODE") == "MASTER"
    )
    peers = sum(
        len(entry.get("PEERS") or {})
        for entry in obj.values()
        if isinstance(entry, dict) and entry.get("MODE") == "MASTER"
    )
    if masters or peers:
        return f"pickle masters={masters} peers={peers} keys={len(obj)}"
    if obj and all(isinstance(v, list) for v in obj.values()):
        return f"pickle bridge_tables={len(obj)} legs={sum(len(v) for v in obj.values())}"
    return f"pickle keys={len(obj)}"


def summarize_upstream(opcode: bytes, payload: bytes) -> str:
    op = opcode_name(opcode)
    if opcode in (
        Opcode.HELLO,
        Opcode.STATE_SND,
        Opcode.TOPOLOGY_SND,
        Opcode.ROUTING_TABLE_SND,
        Opcode.VOICE_EVENT_SND,
        Opcode.DELTA_SND,
    ):
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"{op} {len(payload)} B (invalid JSON)"
        if isinstance(doc, dict):
            return f"{op} {_json_type_summary(doc)}"
        return f"{op} JSON (not an object)"
    if opcode in (Opcode.CONFIG_SND, Opcode.BRIDGE_SND):
        return f"{op} {_pickle_summary(payload)}"
    if opcode == Opcode.BRDG_EVENT:
        text = payload.decode("utf-8", errors="replace")
        return f"{op} {text[:120]}{'…' if len(text) > 120 else ''}"
    if opcode == Opcode.LINK_EVENT:
        return f"{op} {len(payload)} B"
    return f"{op} {len(payload)} B"


def summarize_legacy_request(opcode: bytes) -> str:
    return opcode_name(opcode)


def summarize_legacy_out_frames(frames: list[bytes]) -> str:
    if not frames:
        return "(none)"
    parts: list[str] = []
    for frame in frames:
        op = frame[:1]
        body = frame[1:]
        if op in (Opcode.CONFIG_SND, Opcode.BRIDGE_SND):
            parts.append(f"{opcode_name(op)} {_pickle_summary(body)}")
        elif op == Opcode.BRDG_EVENT:
            text = body.decode("utf-8", errors="replace")
            parts.append(f"BRDG_EVENT {text[:60]}{'…' if len(text) > 60 else ''}")
        else:
            parts.append(f"{opcode_name(op)} {len(body)} B")
    return " | ".join(parts)


def summarize_snapshot(frames: list[bytes]) -> str:
    if not frames:
        return "empty"
    return ", ".join(
        f"{opcode_name(frame[:1])} ({_pickle_summary(frame[1:])})"
        if frame[:1] in (Opcode.CONFIG_SND, Opcode.BRIDGE_SND)
        else opcode_name(frame[:1])
        for frame in frames
    )
