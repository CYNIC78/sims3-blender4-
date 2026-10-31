"""Чтение и запись ресурса CLIP (TypeID 0x6B20C4F3) The Sims 3.

Формат восстановлен по спецификации SimsWiki (Sims 3:0x6B20C4F3) и по
рабочей реализации s3py (s3py.animation.Clip / ClipResource).

Иерархия данных:

    ресурс CLIP
      └── main header (56 байт, ссылки — относительно самого поля-оффсета)
            └── секция _S3Clip_ (заголовок клипа)
                  ├── список кривых (CurveDataInfo): key джойнта, offset/scale,
                  │   число кадров, тип упаковки
                  ├── таблица indexed-float'ов
                  ├── имя анимации + имя исходного файла
                  └── данные кадров (по ним идут ссылки из кривых)

Значения в кадрах — не «готовые» позы, а запакованные числа:
    pos = raw * scale + offset
При этом у джойнта независимые кривые позиции и ориентации, поэтому кадры
могут быть разряжены:bone «двигается» только на тех кадрах, где есть данные.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CLIP_TID = 0x6B20C4F3
CLIP_TAG = b"_pilC3S_"  # на диске это байт-свопнутая строка "_S3Clip_"
PADDING_CHAR = 0x7E


class ClipReadError(Exception):
    """Файл не является CLIP или повреждён."""


# --------------------------------------------------------------------------
# Типы кривых
# --------------------------------------------------------------------------
CURVE_POSITION = 0x01
CURVE_ORIENTATION = 0x02
CURVE_MORPH = 0x07

DATA_VECTOR3_PACKED = 0x02
DATA_VECTOR3_INDEXED = 0x03
DATA_VECTOR4_PACKED = 0x04
DATA_SCALAR = 0x05

_FLOATS_PER_FRAME = {
    DATA_SCALAR: 1,
    DATA_VECTOR3_PACKED: 3,
    DATA_VECTOR3_INDEXED: 3,
    DATA_VECTOR4_PACKED: 4,
}
_BITS_PER_FLOAT = {
    DATA_SCALAR: 16,
    DATA_VECTOR3_PACKED: 10,
    DATA_VECTOR3_INDEXED: 10,
    DATA_VECTOR4_PACKED: 12,
}
_PACKED_WORDS = {
    DATA_SCALAR: 1,
    DATA_VECTOR3_PACKED: 1,
    DATA_VECTOR3_INDEXED: 1,
    DATA_VECTOR4_PACKED: 4,
}
_WORD_WIDTH = {
    DATA_SCALAR: 2,
    DATA_VECTOR3_PACKED: 4,
    DATA_VECTOR3_INDEXED: 2,
    DATA_VECTOR4_PACKED: 2,
}


# --------------------------------------------------------------------------
# Модель данных
# --------------------------------------------------------------------------
@dataclass
class ClipTrack:
    """Все кривые одного джойнта (или морф-цели)."""

    key: int  # FNV32 джойнта, либо (FNV64 имени шейпкея & 0xFFFFFFFF) для морфов
    # (frame_index, (x, y, z))
    positions: List[Tuple[int, Tuple[float, float, float]]] = field(default_factory=list)
    # (frame_index, (x, y, z, w)) — порядок как в игре, xyzw
    orientations: List[Tuple[int, Tuple[float, float, float, float]]] = field(default_factory=list)
    # (frame_index, (value,))
    morphs: List[Tuple[int, Tuple[float, ...]]] = field(default_factory=list)

    @property
    def is_morph_only(self) -> bool:
        return bool(self.morphs) and not (self.positions or self.orientations)

    @property
    def last_frame(self) -> int:
        frames = [f for f, _ in self.positions]
        frames += [f for f, _ in self.orientations]
        frames += [f for f, _ in self.morphs]
        return max(frames) if frames else 0

    def __str__(self) -> str:  # pragma: no cover - отладочное
        return "{%08X} pos:%d rot:%d morph:%d" % (
            self.key,
            len(self.positions),
            len(self.orientations),
            len(self.morphs),
        )


@dataclass
class ClipAnim:
    name: str = ""
    source_file: str = ""
    actor_name: str = ""
    frame_duration: float = 1.0 / 30.0
    max_frame_count: int = 0
    version: int = 2
    tracks: Dict[int, ClipTrack] = field(default_factory=dict)
    # порядок двух float'ов в описании кривой: 'offset_scale' (спецификация/s3py)
    # или 'scale_offset' (встречается в файлах игры)
    field_order: str = "offset_scale"
    # служебное
    path: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def fps(self) -> float:
        if self.frame_duration <= 0:
            return 30.0
        return 1.0 / self.frame_duration

    @property
    def frame_end(self) -> int:
        last = 0
        for t in self.tracks.values():
            last = max(last, t.last_frame)
        return max(last, max(0, self.max_frame_count - 1))

    @property
    def joint_tracks(self) -> Dict[int, ClipTrack]:
        return {k: t for k, t in self.tracks.items() if not t.is_morph_only}


# --------------------------------------------------------------------------
# Низкоуровневые помощники
# --------------------------------------------------------------------------
def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _f32(buf: bytes, off: int) -> float:
    return struct.unpack_from("<f", buf, off)[0]


def _zstring(buf: bytes, off: int, limit: Optional[int] = None) -> str:
    end = buf.find(b"\x00", off)
    if end < 0:
        end = len(buf)
    if limit is not None:
        end = min(end, limit)
    return buf[off:end].decode("latin-1", "replace")


def _signs(word: int) -> int:
    """Биты знаков компонент из слова флагов кадра.

    Игра (и все клипы из игры) держат знаки в МЛАДШЕМ ниббле: бит i — компонента i
    отрицательная. В старых файлах, которые писал этот аддон, знаки лежали в
    старшем ниббле, поэтому если младший пуст, а старший нет — читаем старший.
    """
    low = word & 0x0F
    if low == 0:
        return (word >> 4) & 0x0F
    return low


def _unpack_frame(
    buf: bytes,
    off: int,
    data_type: int,
    offset: float,
    scale: float,
    indexed_floats: List[float],
) -> Tuple[int, int, List[float]]:
    """Читает один кадр. Возвращает (frame_index, flags, values)."""
    frame_index = _u16(buf, off)
    flags = _signs(_u16(buf, off + 2))
    off += 4

    count = _FLOATS_PER_FRAME[data_type]
    values: List[float] = []

    if data_type == DATA_VECTOR3_INDEXED:
        for i in range(count):
            idx = _u16(buf, off)
            off += 2
            val = indexed_floats[idx] if idx < len(indexed_floats) else 0.0
            if flags & (1 << i):
                val = -val
            values.append(val * scale + offset)
        return frame_index, flags, values

    bits = _BITS_PER_FLOAT[data_type]
    max_val = float((1 << bits) - 1)
    floats_per_word = count // _PACKED_WORDS[data_type]
    width = _WORD_WIDTH[data_type]
    for word in range(_PACKED_WORDS[data_type]):
        packed = _u16(buf, off) if width == 2 else _u32(buf, off)
        off += width
        for k in range(floats_per_word):
            shift = k * bits
            raw = (packed >> shift) & int(max_val)
            val = raw / max_val
            fi = word + k
            if flags & (1 << fi):
                val = -val
            values.append(val * scale + offset)
    return frame_index, flags, values


# --------------------------------------------------------------------------
# Парсер
# --------------------------------------------------------------------------
FIELD_ORDER_AUTO = "auto"
FIELD_ORDER_OFFSET_SCALE = "offset_scale"
FIELD_ORDER_SCALE_OFFSET = "scale_offset"
FIELD_ORDERS = (FIELD_ORDER_OFFSET_SCALE, FIELD_ORDER_SCALE_OFFSET)


def parse_clip_bytes(data: bytes, field_order: str = FIELD_ORDER_AUTO) -> ClipAnim:
    """Разбирает как полный ресурс CLIP, так и «голую» секцию _S3Clip_.

    `field_order`: 'auto' — попробовать оба порядка float'ов (offset/scale) и
    выбрать тот, при котором кватернионы выходят единичной длины.
    """
    if len(data) < 64:
        raise ClipReadError("файл слишком мал для CLIP (%d байт)" % len(data))

    has_resource_header = _u32(data, 0) == CLIP_TID
    tag_pos = data.find(CLIP_TAG)
    if not has_resource_header and tag_pos < 0:
        raise ClipReadError("это не CLIP-файл (нет ни TID 0x6B20C4F3, ни тега _S3Clip_)")

    if field_order != FIELD_ORDER_AUTO:
        return _parse_any(data, has_resource_header, tag_pos, field_order)

    best = None
    for order in FIELD_ORDERS:
        try:
            anim = _parse_any(data, has_resource_header, tag_pos, order)
        except ClipReadError:
            continue
        score = _parse_quality(anim)
        if best is None or score < best[0] - 1e-9:
            best = (score, order, anim)
    if best is None:
        raise ClipReadError("не удалось разобрать CLIP")
    score, order, anim = best
    anim.field_order = order
    if order == FIELD_ORDER_SCALE_OFFSET:
        anim.warnings.append(
            "в кривых сначала идёт scale, потом offset (не как в спецификации) — "
            "порядок определён автоматически"
        )
    return anim


def _parse_any(data: bytes, has_resource_header: bool, tag_pos: int, field_order: str) -> ClipAnim:
    if has_resource_header:
        anim = _parse_resource(data, field_order)
    else:
        anim = _parse_clip_section(data, tag_pos, field_order)
        anim.warnings.append("нет заголовка ресурса (0x6B20C4F3) — прочитана голая секция _S3Clip_")
    anim.field_order = field_order
    return anim


def _parse_quality(anim: ClipAnim) -> float:
    """Чем ближе к 0, тем правдоподобнее разбор.

    При перепутанном порядке полей каждая компонента кривой декодируется одним
    и тем же числом, поэтому:

    * позиции становятся «равносторонними» (x == y == z, отсюда ровно √3 × rest);
    * у кватернионов тоже все компоненты одинаковые, а длина уезжает от 1.

    Оба признака считаем долей подозрительных значений и берём худший.
    """
    quat_bad = 0
    quat_total = 0
    norms = []
    for track in anim.tracks.values():
        for _frame, quat in track.orientations[:32]:
            quat_total += 1
            x, y, z, w = quat
            norm = (x * x + y * y + z * z + w * w) ** 0.5
            norms.append(norm)
            degenerate = max(x, y, z) - min(x, y, z) < 1e-3
            if degenerate or abs(norm - 1.0) > 0.25:
                quat_bad += 1

    pos_bad = 0
    pos_total = 0
    for track in anim.tracks.values():
        for _frame, pos in track.positions[:32]:
            pos_total += 1
            if max(pos) - min(pos) < 1e-3:
                pos_bad += 1

    scores = []
    if quat_total:
        scores.append(quat_bad / float(quat_total))
    if pos_total:
        scores.append(pos_bad / float(pos_total))
    if not scores:
        return 0.0
    return max(scores)


def _parse_resource(data: bytes, field_order: str = FIELD_ORDER_OFFSET_SCALE) -> ClipAnim:
    linked = _u32(data, 4)
    if linked:
        raise ClipReadError("CLIP со ссылкой на другой клип (linked clip) не поддерживается")
    clip_size = _u32(data, 8)

    clip_off = _u32(data, 12)
    if not clip_off:
        raise ClipReadError("нулевой оффсет секции клипа")
    clip_start = 12 + clip_off
    if clip_start + 8 > len(data):
        raise ClipReadError("оффсет секции клипа за пределами файла")

    end = len(data) if not clip_size else min(len(data), clip_start + clip_size)
    anim = _parse_clip_section(data[:end], clip_start, field_order)

    actor_off = _u32(data, 20)
    if actor_off:
        pos = 20 + actor_off
        if pos < len(data):
            anim.actor_name = _zstring(data, pos)
    anim.path = ""
    return anim


def _parse_clip_section(data: bytes, start: int, field_order: str = FIELD_ORDER_OFFSET_SCALE) -> ClipAnim:
    if data[start : start + 8] != CLIP_TAG:
        raise ClipReadError("ожидался тег _S3Clip_ по оффсету %d" % start)

    base = start
    version = _u32(data, base + 8)
    frame_duration = _f32(data, base + 16)
    max_frame_count = _u16(data, base + 20)
    curve_count = _u32(data, base + 24)
    float_count = _u32(data, base + 28)
    curves_off = _u32(data, base + 32)
    frames_off = _u32(data, base + 36)
    name_off = _u32(data, base + 40)
    src_name_off = _u32(data, base + 44)

    anim = ClipAnim(
        version=version,
        frame_duration=frame_duration if frame_duration > 0 else 1.0 / 30.0,
        max_frame_count=max_frame_count,
    )
    if curve_count > 1_000_000:
        raise ClipReadError("неправдоподобное число кривых (%d)" % curve_count)

    # индексные float'ы идут в начале секции кадров
    indexed_floats: List[float] = []
    for i in range(min(float_count, 1_000_000)):
        pos = base + frames_off + i * 4
        if pos + 4 > len(data):
            break
        indexed_floats.append(_f32(data, pos))

    if name_off:
        anim.name = _zstring(data, base + name_off)
    if src_name_off:
        anim.source_file = _zstring(data, base + src_name_off)

    # --- кривые ---
    descriptors = []
    pos = base + curves_off
    for _ in range(curve_count):
        if pos + 20 > len(data):
            anim.warnings.append("список кривых обрезан")
            break
        frame_ptr = _u32(data, pos)
        track_key = _u32(data, pos + 4)
        field_a = _f32(data, pos + 8)
        field_b = _f32(data, pos + 12)
        if field_order == FIELD_ORDER_SCALE_OFFSET:
            scale, offset = field_a, field_b
        else:
            offset, scale = field_a, field_b
        frame_count = _u16(data, pos + 16)
        flags = data[pos + 18]
        curve_type = data[pos + 19]
        pos += 20
        descriptors.append((frame_ptr, track_key, offset, scale, frame_count, flags, curve_type))

    for frame_ptr, track_key, offset, scale, frame_count, flags, curve_type in descriptors:
        data_type = (flags & 0x07) >> 0
        static = ((flags & 0x08) >> 3) == 1
        if curve_type not in (CURVE_POSITION, CURVE_ORIENTATION, CURVE_MORPH):
            anim.warnings.append("неизвестный тип кривой 0x%02X у ключа %08X" % (curve_type, track_key))
            continue
        if data_type not in _FLOATS_PER_FRAME:
            if frame_count and not static:
                anim.warnings.append(
                    "неизвестный формат данных 0x%02X у ключа %08X (кадров: %d)"
                    % (data_type, track_key, frame_count)
                )
            continue
        track = anim.tracks.get(track_key)
        if track is None:
            track = ClipTrack(key=track_key)
            anim.tracks[track_key] = track
        if static or frame_count == 0:
            continue

        off = base + frame_ptr
        advance = _frame_advance(data_type)
        for _frame in range(frame_count):
            if off + 4 + advance > len(data):
                anim.warnings.append("данные кадров обрезаны (ключ %08X)" % track_key)
                break
            frame_index, _flags, values = _unpack_frame(
                data, off, data_type, offset, scale, indexed_floats
            )
            off += 4 + advance

            if curve_type == CURVE_POSITION:
                if len(values) >= 3:
                    track.positions.append((frame_index, (values[0], values[1], values[2])))
            elif curve_type == CURVE_ORIENTATION:
                if len(values) >= 4:
                    track.orientations.append(
                        (frame_index, (values[0], values[1], values[2], values[3]))
                    )
            else:
                track.morphs.append((frame_index, tuple(values)))

    for track in anim.tracks.values():
        track.positions.sort(key=lambda kv: kv[0])
        track.orientations.sort(key=lambda kv: kv[0])
        track.morphs.sort(key=lambda kv: kv[0])

    return anim


def _frame_advance(data_type: int) -> int:
    """Сколько байт занимают данные одного кадра (без 4 байт заголовка)."""
    if data_type == DATA_VECTOR3_INDEXED:
        return 3 * 2
    return _PACKED_WORDS[data_type] * _WORD_WIDTH[data_type]


def read_clip(path: str, field_order: str = FIELD_ORDER_AUTO) -> ClipAnim:
    with open(path, "rb") as fh:
        data = fh.read()
    anim = parse_clip_bytes(data, field_order)
    anim.path = path
    return anim


def read_clip_variants(path: str) -> Dict[str, ClipAnim]:
    """Разбирает файл обоими порядками полей — нужно для авто-выбора по ригу."""
    with open(path, "rb") as fh:
        data = fh.read()
    variants: Dict[str, ClipAnim] = {}
    for order in FIELD_ORDERS:
        try:
            anim = parse_clip_bytes(data, order)
        except ClipReadError:
            continue
        anim.path = path
        variants[order] = anim
    if not variants:
        raise ClipReadError("не удалось разобрать CLIP ни одним порядком полей")
    return variants
