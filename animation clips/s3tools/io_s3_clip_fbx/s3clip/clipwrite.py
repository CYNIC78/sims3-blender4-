"""Запись ресурса CLIP (0x6B20C4F3) — нужна для тестов и для будущего
экспорта FBX/DCC → .animation.

Раскладка повторяет ту, которую пишет s3py (ClipResource.write), поэтому
файлы читаются как этим модулем, так и оригинальным инструментом.
"""

from __future__ import annotations

import math
import struct
from typing import List, Tuple

from .clipfile import (
    CLIP_TAG,
    CLIP_TID,
    CURVE_MORPH,
    CURVE_ORIENTATION,
    CURVE_POSITION,
    DATA_SCALAR,
    DATA_VECTOR3_PACKED,
    DATA_VECTOR4_PACKED,
    ClipAnim,
    PADDING_CHAR,
)

EVENT_TAG = b"=CE="


def _pad(buf: bytearray) -> None:
    while len(buf) % 4:
        buf.append(PADDING_CHAR)


def _offset_scale(values: List[float]) -> Tuple[float, float]:
    """Как в CurveDataInfo.process_frames: min/max считаются вместе с нулём."""
    lo = 0.0
    hi = 0.0
    for v in values:
        lo = min(lo, v)
        hi = max(hi, v)
    offset = (lo + hi) / 2.0
    scale = (lo - hi) / 2.0
    if scale == 0.0:
        scale = 1.0
    return offset, scale


def _pack_frame(values, offset, scale, data_type, frame_index):
    """Кодирует один кадр. Возвращает (bytes frame, tuple floats)."""
    if data_type == DATA_VECTOR3_PACKED:
        bits, words, width = 10, 1, 4
    elif data_type == DATA_VECTOR4_PACKED:
        bits, words, width = 12, 4, 2
    else:
        bits, words, width = 16, 1, 2

    max_val = float((1 << bits) - 1)
    per_word = len(values) // words
    flags = 0
    packed_words = [0] * words
    raw_values = []
    for i, v in enumerate(values):
        raw = (v - offset) / scale if scale else 0.0
        if raw < 0:
            flags |= 1 << i
            raw = -raw
        raw_values.append(raw)
    for w in range(words):
        packed_word = 0
        for k in range(per_word):
            idx = w + k
            q = int(math.floor(raw_values[idx] * max_val + 0.5))
            q = max(0, min(int(max_val), q))
            packed_word |= q << (k * bits)
        packed_words[w] = packed_word

    out = struct.pack("<HH", frame_index, flags & 0x0F)
    for pw in packed_words:
        out += struct.pack("<H" if width == 2 else "<I", pw)
    return out


def build_clip_section(anim: ClipAnim, field_order: str = "offset_scale") -> bytes:
    """Собирает секцию _S3Clip_ (без внешнего заголовка ресурса)."""
    curves = []  # (track_key, curve_type, data_type, frames, offset, scale)
    float_values: List[float] = []

    for key, track in sorted(anim.tracks.items()):
        if track.positions:
            frames = [(f, list(v)) for f, v in track.positions]
            values = [c for _f, v in frames for c in v]
            off, scl = _offset_scale(values)
            curves.append((key, CURVE_POSITION, DATA_VECTOR3_PACKED, frames, off, scl))
        if track.orientations:
            frames = [(f, list(v)) for f, v in track.orientations]
            values = [c for _f, v in frames for c in v]
            off, scl = _offset_scale(values)
            curves.append((key, CURVE_ORIENTATION, DATA_VECTOR4_PACKED, frames, off, scl))
        if track.morphs:
            frames = [(f, list(v)) for f, v in track.morphs]
            values = [c for _f, v in frames for c in v]
            off, scl = _offset_scale(values)
            curves.append((key, CURVE_MORPH, DATA_SCALAR, frames, off, scl))

    header_size = 48
    descriptor_size = 20 * len(curves)
    name_bytes = anim.name.encode("latin-1", "replace") + b"\x00"
    src_bytes = (anim.source_file or "").encode("latin-1", "replace") + b"\x00"

    curves_offset = header_size
    name_offset = curves_offset + descriptor_size
    src_offset = name_offset + len(name_bytes)
    frames_offset = src_offset + len(src_bytes)

    # сначала кодируем кадры, чтобы знать сдвиги
    frame_blobs = []
    cursor = frames_offset
    for key, curve_type, data_type, frames, off, scl in curves:
        frame_ptr = cursor
        blob = b""
        for f, vals in frames:
            blob += _pack_frame(vals, off, scl, data_type, f)
        frame_blobs.append((frame_ptr, blob))
        cursor += len(blob)

    out = bytearray()
    out += CLIP_TAG
    out += struct.pack("<I", anim.version)
    out += struct.pack("<I", 0)  # unknown1
    out += struct.pack("<f", anim.frame_duration if anim.frame_duration else 1.0 / 30.0)
    max_frames = max([anim.max_frame_count, anim.frame_end + 1] + [0])
    out += struct.pack("<HH", min(0xFFFF, max_frames), 0x6C73)
    out += struct.pack("<I", len(curves))
    out += struct.pack("<I", 0)  # indexed float'ов не пишем
    out += struct.pack("<I", curves_offset)
    out += struct.pack("<I", frames_offset)
    out += struct.pack("<I", name_offset)
    out += struct.pack("<I", src_offset)
    assert len(out) == header_size

    for (key, curve_type, data_type, frames, off, scl), (frame_ptr, _blob) in zip(
        curves, frame_blobs
    ):
        flags = data_type & 0x07
        if curve_type in (CURVE_POSITION, CURVE_ORIENTATION):
            flags |= 0x10
        if field_order == "scale_offset":
            out += struct.pack("<IIffHBB", frame_ptr, key, scl, off, len(frames), flags, curve_type)
        else:
            out += struct.pack("<IIffHBB", frame_ptr, key, off, scl, len(frames), flags, curve_type)

    out += name_bytes
    out += src_bytes
    for _frame_ptr, blob in frame_blobs:
        out += blob
    return bytes(out)


def build_clip_resource(anim: ClipAnim, field_order: str = "offset_scale") -> bytes:
    """Собирает полный ресурс CLIP (то, что лежит в .animation / package)."""
    clip_section = build_clip_section(anim, field_order)

    header_size = 56
    clip_ptr_pos = 12
    ik_ptr_pos = 16
    actor_ptr_pos = 20
    event_ptr_pos = 24
    vector_ptr_pos = 36

    out = bytearray()
    out += struct.pack("<I", CLIP_TID)
    out += struct.pack("<I", 0)  # linked clip
    out += struct.pack("<I", len(clip_section))
    out += struct.pack("<I", 0)  # clip offset (заполним ниже)
    out += struct.pack("<I", 0)  # ik offset (нет секции IK)
    out += struct.pack("<I", 0)  # actor offset (заполним ниже)
    out += struct.pack("<I", 0)  # event offset (заполним ниже)
    out += struct.pack("<I", 0)  # unknown1
    out += struct.pack("<I", 1)  # unknown2 (в игре всегда 1)
    out += struct.pack("<I", 0)  # vector offset (заполним ниже)
    out += b"\x00" * 16
    assert len(out) == header_size

    struct.pack_into("<I", out, clip_ptr_pos, len(out) - clip_ptr_pos)
    out += clip_section
    _pad(out)

    actor_name = (anim.actor_name or "a").encode("latin-1", "replace") + b"\x00"
    struct.pack_into("<I", out, actor_ptr_pos, len(out) - actor_ptr_pos)
    out += actor_name
    _pad(out)

    struct.pack_into("<I", out, event_ptr_pos, len(out) - event_ptr_pos)
    out += EVENT_TAG
    out += struct.pack("<I", 0x00000103)
    out += struct.pack("<i", 0)  # cEvents
    out += struct.pack("<I", 0)  # length
    out += struct.pack("<I", 0)  # start offset
    _pad(out)

    struct.pack_into("<I", out, vector_ptr_pos, len(out) - vector_ptr_pos)
    out += struct.pack("<4f", 0.0, 0.0, 0.0, 1.0)

    # оффсет IK = 0 (секции нет)
    struct.pack_into("<I", out, ik_ptr_pos, 0)
    return bytes(out)


def write_clip(path: str, anim: ClipAnim, field_order: str = "offset_scale") -> None:
    with open(path, "wb") as fh:
        fh.write(build_clip_resource(anim, field_order))
