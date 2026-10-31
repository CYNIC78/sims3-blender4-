"""Диагностика клипа: почему поза «взорвалась» и какая в файле конвенция.

Две вещи:

1. `analyze_clip()` — сравнивает данные клипа с rest-позой рига и говорит,
   хранятся ли позиции/повороты как «абсолютные parent-relative» значения
   (как их пишет оригинальный плагин) или как дельты от rest-позы.

2. `fk_check()` — прогоняет бейк и делает прямой кинематический расчёт:
   какие кости уехали от своего rest-положения сильнее всего. Сразу видно,
   врёт позиция, поворот или конкретный трек.

Модуль без bpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import mat
from .bake import BakeOptions, Skeleton, bake_clip
from .clipfile import FIELD_ORDER_OFFSET_SCALE, ClipAnim
from .fnv import fnv1_32


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return 0.5 * (values[mid - 1] + values[mid])


def _vec_len(v) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _quat_angle_deg(q) -> float:
    """Угол поворота кватерниона (xyzw) в градусах."""
    w = max(-1.0, min(1.0, abs(q[3])))
    return math.degrees(2.0 * math.acos(w))


@dataclass
class TrackStat:
    key: int
    bone: Optional[str]
    frames: int
    pos_count: int
    rot_count: int
    rest_pos_len: float = 0.0
    clip_pos_len: float = 0.0
    rest_rot_deg: float = 0.0
    clip_rot_deg: float = 0.0
    notes: List[str] = field(default_factory=list)


@dataclass
class ClipAnalysis:
    name: str
    fps: float
    declared_frames: int
    actual_frames: int
    tracks_total: int
    tracks_matched: int
    tracks_with_position: int
    morph_tracks: int
    pos_ratio: Optional[float]
    rot_identity_share: Optional[float]
    quat_norm_error: float = 0.0
    degenerate_pos_share: float = 0.0
    verdict: List[str] = field(default_factory=list)
    sample: List[TrackStat] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def analyze_clip(anim: ClipAnim, skeleton: Skeleton) -> ClipAnalysis:
    hashes = skeleton.hash_map()
    offsets = skeleton.offsets()

    stats: List[TrackStat] = []
    pos_ratios: List[float] = []
    identity_votes = 0
    rot_votes = 0
    morph = 0

    for key, track in anim.tracks.items():
        if track.is_morph_only:
            morph += 1
            continue
        index = hashes.get(key)
        stat = TrackStat(
            key=key,
            bone=skeleton.joints[index].name if index is not None else None,
            frames=track.last_frame + 1,
            pos_count=len(track.positions),
            rot_count=len(track.orientations),
        )
        if index is not None:
            offset = offsets[index]
            stat.rest_pos_len = _vec_len(mat.to_translation(offset))
            stat.rest_rot_deg = _quat_angle_deg(mat.to_quat(offset))
            if track.positions:
                stat.clip_pos_len = _median([_vec_len(v) for _f, v in track.positions])
                if stat.rest_pos_len > 1e-4:
                    pos_ratios.append(stat.clip_pos_len / stat.rest_pos_len)
            if track.orientations:
                stat.clip_rot_deg = _median([_quat_angle_deg(v) for _f, v in track.orientations])
                if stat.rest_rot_deg > 20.0:
                    rot_votes += 1
                    if stat.clip_rot_deg < 10.0:
                        identity_votes += 1
            # грубые аномалии
            if track.positions and stat.clip_pos_len > 20.0:
                stat.notes.append("очень большая позиция (%.2f)" % stat.clip_pos_len)
            if track.orientations:
                bad = [v for _f, v in track.orientations
                       if abs(sum(c * c for c in v) - 1.0) > 0.05]
                if bad:
                    stat.notes.append("кватернион не единичной длины (%.2f)" %
                                      math.sqrt(sum(c * c for c in bad[0])))
        stats.append(stat)

    pos_ratio = _median(pos_ratios) if pos_ratios else None
    rot_identity_share = (identity_votes / rot_votes) if rot_votes else None

    # «здоровье» самого разбора: так ловятся перепутанные знаки/порядок полей
    norm_error = 0.0
    for track in anim.tracks.values():
        for _frame, quat in track.orientations:
            norm_error = max(norm_error, abs(math.sqrt(sum(c * c for c in quat)) - 1.0))
    triples = [p for track in anim.tracks.values() for _f, p in track.positions]
    degenerate = (
        sum(1 for p in triples if max(p) - min(p) < 1e-3) / float(len(triples))
        if triples else 0.0
    )

    analysis = ClipAnalysis(
        name=anim.name,
        fps=anim.fps,
        declared_frames=anim.max_frame_count,
        actual_frames=anim.frame_end + 1,
        tracks_total=len(anim.tracks),
        tracks_matched=sum(1 for s in stats if s.bone),
        tracks_with_position=sum(1 for s in stats if s.pos_count),
        morph_tracks=morph,
        pos_ratio=pos_ratio,
        rot_identity_share=rot_identity_share,
        quat_norm_error=norm_error,
        degenerate_pos_share=degenerate,
        warnings=list(anim.warnings),
    )

    for stat in stats:
        stat.notes and analysis.warnings.append(
            "%s: %s" % (stat.bone or "%08X" % stat.key, "; ".join(stat.notes))
        )

    # ── вердикт по самому разбору файла ──────────────────────────────────
    if norm_error <= 0.02 and degenerate <= 0.05:
        analysis.verdict.append(
            "Разбор файла в порядке: все кватернионы единичные (отклонение %.4f), "
            "позиции не вырождены." % norm_error
        )
    else:
        analysis.verdict.append(
            "РАЗБОР ФАЙЛА ПОДОЗРИТЕЛЕН: отклонение длины кватерниона %.3f, доля "
            "позиций «x=y=z» %.0f%%. Это не про конвенцию — дело в чтении кривых "
            "(знаки/упаковка/порядок полей)."
            % (norm_error, degenerate * 100)
        )

    # ── вердикт по позициям ──────────────────────────────────────────────
    if pos_ratio is None:
        analysis.verdict.append(
            "В клипе нет кривых позиции — конвенция позиций не проверится, "
            "но и испортить позу ими нельзя."
        )
    elif 0.55 <= pos_ratio <= 1.8:
        analysis.verdict.append(
            "Позиции похожи на АБСОЛЮТНЫЕ parent-relative (как в rest-позе): "
            "режим «absolute» подходит (отношение %.2f)." % pos_ratio
        )
    elif pos_ratio < 0.4:
        analysis.verdict.append(
            "Позиции МАЛЕНЬКИЕ по сравнению с rest-смещениями (отношение %.2f): "
            "похоже, это ДЕЛЬТА от rest-позы. Попробуйте «Позиции: дельта от rest»."
            % pos_ratio
        )
    else:
        analysis.verdict.append(
            "Позиции неоднозначны (отношение %.2f) — попробуйте оба режима."
            % pos_ratio
        )

    # ── вердикт по поворотам ─────────────────────────────────────────────
    if rot_identity_share is None:
        analysis.verdict.append("Повороты: достаточно данных для вывода нет.")
    elif rot_identity_share > 0.5:
        analysis.verdict.append(
            "%.0f%% костей с большим rest-поворотом имеют в клипе почти единичный "
            "кватернион → повороты, похоже, хранятся как ДЕЛЬТА от rest. "
            "Попробуйте «Повороты: дельта от rest»." % (rot_identity_share * 100)
        )
    else:
        analysis.verdict.append(
            "Повороты выглядят абсолютными (доля «единичных» %.0f%%)."
            % (rot_identity_share * 100)
        )

    # ── проверка самого разбора: длины кватернионов ───────────────────────
    bad_norms = []
    for track in anim.tracks.values():
        for _frame, quat in track.orientations[:32]:
            norm = math.sqrt(sum(c * c for c in quat))
            if abs(norm - 1.0) > 0.02:
                bad_norms.append(norm)
    if bad_norms:
        analysis.verdict.append(
            "ВНИМАНИЕ: %d кватернионов не единичной длины (например %.2f). Это признак "
            "того, что разбор файла неверный, а не позы." % (len(bad_norms), bad_norms[0])
        )

    stats.sort(key=lambda s: (is_service_bone(s.bone or ""),
                              -max(s.clip_pos_len, s.rest_pos_len)))
    analysis.sample = [s for s in stats if s.bone][:12]
    return analysis


SERVICE_BONE_MARKERS = (
    "Info",
    "ExportPole",
    "slotOffset",
    "rootOffset",
    "footOffset",
    "World",
)


def is_service_bone(name: str) -> bool:
    """Служебные кости рига: в игре они стоят не там, где в риге.

    Их позиция/поворот в клипе не совпадает с rest-позой рига, но на позу
    персонажа это не влияет (игра их не рисует). В отчётах их надо отделять,
    иначе «самая уехавшая кость» — всегда какая-нибудь L_Info1.
    """
    if not name:
        return False
    return any(marker in name for marker in SERVICE_BONE_MARKERS)


@dataclass
class FkOffender:
    bone: str
    max_shift: float
    rest_len: float
    frame: int
    service: bool = False


def fk_check(
    anim: ClipAnim,
    skeleton: Skeleton,
    options: Optional[BakeOptions] = None,
    frames: int = 5,
) -> List[FkOffender]:
    """Прямая кинематика по результату бейка: кто уехал дальше всех.

    Смещение сравнивается с длиной кости: если кость уехала на несколько
    своих длин — что-то не так с данными (позиции/повороты/парсер).
    """
    options = options or BakeOptions()
    result = bake_clip(anim, skeleton, options)
    curves = {b.index: b for b in result.bones}
    offsets = skeleton.offsets()

    worst: Dict[int, FkOffender] = {}

    def basis_of(index: int, frame: int):
        entry = curves.get(index)
        if entry is None:
            return mat.identity(), mat.identity()
        t_mat = mat.identity()
        r_mat = mat.identity()
        if entry.positions:
            pos = entry.positions[0][1]
            for f, p in entry.positions:
                if f <= frame:
                    pos = p
                else:
                    break
            t_mat = mat.translation(pos)
        if entry.rotations:
            quat = entry.rotations[0][1]
            for f, q in entry.rotations:
                if f <= frame:
                    quat = q
                else:
                    break
            r_mat = mat.from_quat(quat)
        return t_mat, r_mat

    check_frames = [result.frame_start + i for i in range(frames)]
    for frame in check_frames:
        pose: Dict[int, tuple] = {}
        for index, joint in enumerate(skeleton.joints):
            parent_pose = pose[joint.parent] if joint.parent >= 0 else mat.identity()
            t_mat, r_mat = basis_of(index, frame)
            offset = offsets[index]
            if joint.parent < 0:
                local = offset
            else:
                local = offset
            channel = mat.mul(local, mat.mul(t_mat, r_mat))
            pose[index] = mat.mul(parent_pose, channel)

            head = mat.to_translation(pose[index])
            rest_head = mat.to_translation(joint.matrix_local)
            shift = _vec_len((head[0] - rest_head[0], head[1] - rest_head[1], head[2] - rest_head[2]))
            rest_len = max(1e-4, _vec_len(mat.to_translation(offset)))
            current = worst.get(index)
            if current is None or shift > current.max_shift:
                worst[index] = FkOffender(
                    joint.name, shift, rest_len, frame, is_service_bone(joint.name)
                )

    out = sorted(worst.values(), key=lambda o: (o.service, -o.max_shift))
    return out


def worst_shift(offenders: List[FkOffender]) -> float:
    """Максимальный сдвиг только по «настоящим» костям скелета."""
    values = [o.max_shift for o in offenders if not o.service]
    if not values:
        values = [o.max_shift for o in offenders]
    return max(values, default=0.0)


# --------------------------------------------------------------------------
# Автоопределение конвенции
# --------------------------------------------------------------------------
_CONVENTIONS = (
    ("absolute", "absolute"),
    ("delta", "absolute"),
    ("absolute", "delta"),
    ("delta", "delta"),
)


def detect_convention(anim: ClipAnim, skeleton: Skeleton, base_options: Optional[BakeOptions] = None):
    """Перебирает 4 конвенции и возвращает ту, при которой поза ближе к rest.

    Возвращает (position_mode, rotation_mode, scores) где scores — максимальный
    сдвиг костей от rest для каждой конвенции (меньше = правдоподобнее).
    Порядок перебора начинается с «как в оригинале», поэтому при равенстве
    предпочитается он.
    """
    base_options = base_options or BakeOptions()
    scores = {}
    best = None
    for pos_mode, rot_mode in _CONVENTIONS:
        options = BakeOptions(
            dense=True,
            interpolate=base_options.interpolate,
            frame_start=base_options.frame_start,
            frame_end=min(2, anim.frame_end),
            missing_position=base_options.missing_position,
            root_rotation=base_options.root_rotation,
            position_mode=pos_mode,
            rotation_mode=rot_mode,
            scale=base_options.scale,
        )
        offenders = fk_check(anim, skeleton, options, frames=2)
        score = worst_shift(offenders)
        scores[(pos_mode, rot_mode)] = score
        if best is None or score < scores[best] - 1e-6:
            best = (pos_mode, rot_mode)
    return best[0], best[1], scores


def detect_field_order(
    variants: Dict[str, ClipAnim],
    skeleton: Skeleton,
    base_options: Optional[BakeOptions] = None,
    convention: Optional[Tuple[str, str]] = None,
):
    """Выбирает порядок полей (offset_scale / scale_offset) по близости позы к rest.

    `convention` — заранее известная (pos_mode, rot_mode); если None, для каждого
    варианта конвенция подбирается автоматически.

    Возвращает (field_order, position_mode, rotation_mode, scores).
    """
    scores: Dict[str, float] = {}
    best = None
    for order, anim in variants.items():
        if convention is None:
            pos_mode, rot_mode, _ = detect_convention(anim, skeleton, base_options)
        else:
            pos_mode, rot_mode = convention
        options = BakeOptions(
            dense=True,
            frame_start=(base_options or BakeOptions()).frame_start,
            frame_end=min(2, anim.frame_end),
            missing_position=(base_options or BakeOptions()).missing_position,
            position_mode=pos_mode,
            rotation_mode=rot_mode,
        )
        offenders = fk_check(anim, skeleton, options, frames=2)
        score = worst_shift(offenders)
        scores[order] = score
        if best is None or score < scores[best[0]] - 1e-6:
            best = (order, pos_mode, rot_mode)
    if best is None:
        raise ValueError("нет вариантов разбора")
    return best[0], best[1], best[2], scores
