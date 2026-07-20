"""
PASSERELLE — Encodage/décodage du protocole Jupyter kernel WebSocket (binaire).

═══════════════════════════════════════════════════════════════════
FORMAT SERVEUR → CLIENT (réception, binary=True depuis Jupyter 2.x)
═══════════════════════════════════════════════════════════════════
Reverse-engineered sur Jupyter Server 2.17.0 / SSPCloud :

  [nparts : uint64_le]              — N sections totales (enveloppe + parties)
  [abs_end_0 : uint64_le]           — fin de l'enveloppe = début des données
  [abs_end_1 : uint64_le]           — fin de la partie 1 (channel)
  ...
  [abs_end_N : uint64_le]           — fin de la dernière partie = taille totale

  Parties (entre offsets consécutifs, en partant de abs_end_0) :
    channel bytes   (ex: b"iopub", b"shell")
    header JSON
    parent_header JSON
    metadata JSON
    content JSON
    [buffers...]

═══════════════════════════════════════════════════════════════════
FORMAT CLIENT → SERVEUR (envoi, depuis jupyter_client.session)
═══════════════════════════════════════════════════════════════════
Source : jupyter_client.session.deserialize_binary_message

  [nbufs : uint32 big-endian]             — nombre de parties
  [size_0 : uint32 big-endian]            — taille de la partie 0
  ...
  [size_n : uint32 big-endian]
  [part_0] ... [part_n]

  Parties (ordre fixe, PAS de channel) :
    header JSON
    parent_header JSON
    metadata JSON
    content JSON
    [buffers...]

Référence : https://jupyter-client.readthedocs.io/en/stable/messaging.html
"""

from __future__ import annotations

import json
import struct
import uuid
from datetime import datetime, timezone
from typing import Any


# ── ENVOI : client → Jupyter ─────────────────────────────────────────────────

def encode_message(
    msg_type: str,
    content: dict,
    session: str,
    channel: str = "shell",
    parent_header: dict | None = None,
    metadata: dict | None = None,
    buffers: list[bytes] | None = None,
) -> bytes:
    """
    Encode un message au format Jupyter WebSocket binaire (confirmé sur JupyterLab 2.17.0).
    Retourne des bytes prêts à envoyer via ws.send_bytes().
    Format : nparts(uint64_le) + offsets_absolus(uint64_le×nparts) + channel + header + ...
    """
    header = {
        "msg_id":   str(uuid.uuid4()),
        "username": "passerelle",
        "session":  session,
        "date":     datetime.now(timezone.utc).isoformat(),
        "msg_type": msg_type,
        "version":  "5.4",
    }

    parts: list[bytes] = [
        channel.encode(),                           # Part 0 : channel ("shell", "iopub"...)
        json.dumps(header).encode(),                # Part 1 : header
        json.dumps(parent_header or {}).encode(),   # Part 2 : parent_header
        json.dumps(metadata or {}).encode(),        # Part 3 : metadata
        json.dumps(content).encode(),               # Part 4 : content
    ]
    if buffers:
        parts.extend(buffers)

    # Format confirmé sur JupyterLab 2.17.0 (identique client↔serveur) :
    # [nparts:uint64_le] [abs_end_0:uint64_le] ... [abs_end_n:uint64_le] [data...]
    # nparts = 1 (enveloppe) + nombre de parties réelles
    nparts = len(parts) + 1
    envelope_size = 8 + nparts * 8  # uint64 nparts + nparts × uint64 offsets

    abs_offsets: list[int] = [envelope_size]  # [fin_enveloppe, fin_part0, ...]
    pos = envelope_size
    for part in parts:
        pos += len(part)
        abs_offsets.append(pos)

    frame = struct.pack("<Q", nparts)
    for off in abs_offsets:
        frame += struct.pack("<Q", off)
    for part in parts:
        frame += part

    return frame


def execute_request(code: str, session: str, silent: bool = False) -> bytes:
    return encode_message("execute_request", {
        "code":             code,
        "silent":           silent,
        "store_history":    not silent,
        "user_expressions": {},
        "allow_stdin":      False,
        "stop_on_error":    True,
    }, session)


def kernel_info_request(session: str) -> bytes:
    return encode_message("kernel_info_request", {}, session)


# ── RÉCEPTION : Jupyter → client ─────────────────────────────────────────────

def decode_message(raw: bytes) -> dict[str, Any]:
    """
    Décode un frame binaire reçu de Jupyter Server 2.x.
    Format : nparts(uint64_le) + abs_end_offsets + channel + header + ... + content
    """
    if len(raw) < 8:
        raise ValueError(f"Frame trop courte : {len(raw)} bytes")

    nparts = struct.unpack_from("<Q", raw, 0)[0]
    if nparts < 2 or nparts > 100:
        raise ValueError(f"nparts invalide : {nparts}")

    # Lire les nparts offsets absolus
    offsets: list[int] = []
    for i in range(nparts):
        offsets.append(struct.unpack_from("<Q", raw, 8 + i * 8)[0])

    # offsets[0] = fin de l'enveloppe = début des données
    # Les parties sont entre offsets consécutifs à partir du début des données
    start = offsets[0]
    boundaries = [start] + offsets[1:]  # CORRECTION : pas offsets[0] dupliqué

    parts: list[bytes] = []
    for i in range(len(boundaries) - 1):
        s, e = boundaries[i], boundaries[i + 1]
        if e > len(raw):
            break
        parts.append(raw[s:e])

    def _j(b: bytes) -> dict:
        try:
            return json.loads(b)
        except Exception:
            return {}

    # Parties : channel | header | parent_header | metadata | content | buffers...
    channel       = parts[0].decode(errors="replace") if len(parts) > 0 else ""
    header        = _j(parts[1]) if len(parts) > 1 else {}
    parent_header = _j(parts[2]) if len(parts) > 2 else {}
    metadata      = _j(parts[3]) if len(parts) > 3 else {}
    content       = _j(parts[4]) if len(parts) > 4 else {}
    extra_buffers = parts[5:] if len(parts) > 5 else []

    return {
        "channel":       channel,
        "header":        header,
        "parent_header": parent_header,
        "metadata":      metadata,
        "content":       content,
        "buffers":       extra_buffers,
        "msg_type":      header.get("msg_type", ""),
    }


def try_decode(raw: bytes | str) -> dict[str, Any] | None:
    """Décode un frame (binaire ZMQ ou JSON texte). Retourne None si invalide."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    try:
        return decode_message(raw)
    except Exception:
        try:
            return json.loads(raw.decode())
        except Exception:
            return None
