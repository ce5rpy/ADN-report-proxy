# ADN Report Proxy - report opcodes
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

"""Report protocol opcodes (single-byte prefix)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Opcode:
    value: bytes

    CONFIG_REQ = b"\x00"
    CONFIG_SND = b"\x01"
    BRIDGE_REQ = b"\x02"
    BRIDGE_SND = b"\x03"
    LINK_EVENT = b"\x06"
    BRDG_EVENT = b"\x07"
    TOPOLOGY_SND = b"\x10"
    ROUTING_TABLE_SND = b"\x11"
    VOICE_EVENT_SND = b"\x12"
    DELTA_SND = b"\x13"
    STATE_SND = b"\x14"
    STATE_REQ = b"\x15"
    HELLO = b"\xff"

    @classmethod
    def from_frame(cls, raw: bytes) -> Opcode:
        return cls(value=raw[:1] if raw else b"")

    def is_v2_only(self) -> bool:
        return self.value in (
            Opcode.TOPOLOGY_SND,
            Opcode.ROUTING_TABLE_SND,
            Opcode.VOICE_EVENT_SND,
            Opcode.DELTA_SND,
            Opcode.STATE_SND,
        )
