"""Преобразование CLIP → локальные трансформы костей (без bpy).

Как это работает
----------------
Игра хранит для каждого джойнта два независимых набора значений:
позицию и ориентацию (кватернион, порядок xyzw) — в системе координат
родителя. Оригинальный плагин кладёт их в позу так:

    pose[joint] = pose[parent] @ Translation(P) @ Rotation(Q)

Blender же хранит локальную «базу» кости (matrix_basis), и

    pose[bone] = pose[parent] @ (rest[parent]^-1 @ rest[bone]) @ basis[bone]

Подставляя одно в другое, родительская поза сокращается, и получается
чистая локальная формула:

    basis[bone] = (rest[parent]^-1 @ rest[bone])^-1 @ Translation(P) @ Rotation(Q)

Для корневой кости (без родителя) вместо offset берётся rest[bone].
Именно это и считает этот модуль — поэтому не нужно ни выставлять позы
по одной, ни обновлять depsgraph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .clipfile import ClipAnim, ClipTrack
from .fnv import fnv1_32
from . import mat, naming
from .mat import Mat4, Quat, Vec3

IDENTITY = mat.identity()

FRAME_INTERP_LINEAR = "linear"
FRAME_INTERP_STEP = "step"


@dataclass
class JointRest:
    """Кость скелета в rest-позе."""

    name: str
    parent: int  # индекс родителя или -1
    matrix_local: Mat4  # bone.matrix_local (в системе арматуры)
    # альтернативные имена (после переименования в .L/.R и т.п.)
    aliases: Tuple[str, ...] = ()


@dataclass
class Skeleton:
    joints: List[JointRest]

    def hash_map(self) -> Dict[int, int]:
        """FNV32-хеш имени (и всех его вариантов) → индекс кости."""
        out: Dict[int, int] = {}
        for index, joint in enumerate(self.joints):
            for name in (joint.name,) + tuple(joint.aliases) + naming.name_variants(joint.name):
                if not name:
                    continue
                key = fnv1_32(name)
                # кость, у которой имя совпало «как есть», важнее алиаса
                if key not in out or name == joint.name:
                    out[key] = index
        return out

    def children_of(self, index: int) -> List[int]:
        return [i for i, joint in enumerate(self.joints) if joint.parent == index]

    def subtree_sizes(self) -> List[int]:
        """Сколько костей (включая саму) в поддереве каждой кости."""
        sizes = [1] * len(self.joints)
        # обход в порядке «дети раньше родителей»
        order = sorted(range(len(self.joints)), key=lambda i: -self._depth(i))
        for index in order:
            for child in self.children_of(index):
                sizes[index] += sizes[child]
        return sizes

    def _depth(self, index: int) -> int:
        depth = 0
        parent = self.joints[index].parent
        while parent >= 0:
            depth += 1
            parent = self.joints[parent].parent
        return depth

    def root_chain(self, split_share: float = 0.2) -> List[int]:
        """Корневая цепочка: от корня вниз, пока скелет не разветвится на две крупные ветви.

        У игровых ригов это `b__ROOT__` → `b__ROOT_bind__`: именно вторая кость
        тащит всё тело, поэтому «позиции только у корня» должны включать обе.
        Останавливаемся, когда у кости есть два ребёнка, каждый из которых
        покрывает хотя бы `split_share` всего скелета (позвоночник и таз).
        """
        sizes = self.subtree_sizes()
        total = max(1, len(self.joints))
        roots = [i for i, joint in enumerate(self.joints) if joint.parent < 0]
        if not roots:
            return []
        chain = [roots[0]]
        current = roots[0]
        while True:
            big = [c for c in self.children_of(current) if sizes[c] >= split_share * total]
            if len(big) >= 2:
                break
            if not big:
                break
            current = big[0]
            chain.append(current)
        return chain

    def root_chain_names(self, split_share: float = 0.2) -> List[str]:
        return [self.joints[i].name for i in self.root_chain(split_share)]

    def joint_by_name(self, name: str) -> int:
        for index, joint in enumerate(self.joints):
            if joint.name == name:
                return index
        return -1

    def offsets(self) -> List[Mat4]:
        """rest[parent]^-1 @ rest[bone] (для корня — просто rest[bone])."""
        out: List[Mat4] = []
        for joint in self.joints:
            rest = joint.matrix_local
            if joint.parent < 0:
                out.append(rest)
            else:
                parent_rest = self.joints[joint.parent].matrix_local
                out.append(mat.mul(mat.inverse_rigid(parent_rest), rest))
        return out


# Что делать с кривыми позиции
POSITIONS_ALL = "all"        # как в клипе (нужно, если риг совпадает с игровым скелетом)
POSITIONS_ROOT = "root"      # только корневая цепочка: остальные живут по своим rest-смещениям
POSITIONS_NONE = "none"      # позиции не импортировать вовсе
# только ДВИЖЕНИЕ: из клипа берётся дельта от rest источника, делённая на его
# «единицу» (высоту bind-корня) и умноженная на единицу целевого рига.
# Ничего не зависит от абсолютной геометрии чужого скелета → работает на риге
# любого роста и пропорций.
POSITIONS_MOTION = "motion"
POSITIONS_MODES = (POSITIONS_ALL, POSITIONS_ROOT, POSITIONS_MOTION, POSITIONS_NONE)

# откуда брать опорное значение кривой
MOTION_REF_FRAME = "frame"    # значение на опорном кадре (по умолчанию 0)
MOTION_REF_MEDIAN = "median"  # медиана по кадру


@dataclass
class BakeOptions:
    dense: bool = True  # ключ на каждом кадре (рекомендуется для FBX)
    interpolate: str = FRAME_INTERP_LINEAR  # линейно между «родными» кадрами
    frame_start: int = 0
    frame_end: Optional[int] = None
    # что подставлять костям, для которых в клипе нет данных позиции
    missing_position: str = "relative"  # 'relative' | 'absolute' (как в оригинале)
    # вращение безродительской кости: 'identity' — как в оригинальном плагине
    root_rotation: str = "identity"
    # конвенция данных: absolute — значения parent-relative как есть (как в оригинале),
    # delta — значения являются дельтой от rest-позы
    position_mode: str = "absolute"  # 'absolute' | 'delta'
    rotation_mode: str = "absolute"  # 'absolute' | 'delta'
    # проверить 4 комбинации и выбрать ту, где поза ближе к rest-позе
    auto_convention: bool = True
    # домножить позиции (масштаб юнитов)
    scale: float = 1.0
    # какие кривые позиции вообще применять (см. POSITIONS_*)
    positions: str = POSITIONS_ALL
    # ручное переопределение «корня»: имя кости, чья трансляция сохраняется
    positions_root_bone: Optional[str] = None
    # --- режим POSITIONS_MOTION ---
    # опорное значение кривой источника: на кадре motion_ref_frame или медиана
    motion_ref: str = MOTION_REF_FRAME
    motion_ref_frame: int = 0
    # «единицы» скелетов (высота bind-корня в rest). 0 — определить автоматически
    motion_unit_source: float = 0.0
    motion_unit_target: float = 0.0
    # общий множитель амплитуд (тонкая подстройка)
    motion_scale: float = 1.0
    # насколько сохранять ГОРИЗОНТАЛЬНОЕ покачивание корня (0…1.5)
    motion_horizontal: float = 1.0
    # переносить движение служебных костей (Info/ExportPole/slotOffset)
    motion_service: bool = False
    # после бейка прижать персонажа к полу (min Z стоп = 0)
    ground_snap: bool = False


@dataclass
class BoneCurves:
    index: int
    name: str
    positions: List[Tuple[int, Vec3]] = field(default_factory=list)
    rotations: List[Tuple[int, Quat]] = field(default_factory=list)


@dataclass
class BakeResult:
    frame_start: int
    frame_end: int
    bones: List[BoneCurves]
    unmatched_hashes: List[int]
    morph_tracks: List[ClipTrack]
    fps: float
    warnings: List[str] = field(default_factory=list)

    @property
    def matched_bones(self) -> int:
        return len(self.bones)


# --------------------------------------------------------------------------
def _sample(
    keys: Sequence[Tuple[int, Sequence[float]]],
    frame: int,
    mode: str,
    is_rotation: bool,
):
    if not keys:
        return None
    if frame <= keys[0][0]:
        return tuple(keys[0][1])
    if frame >= keys[-1][0]:
        return tuple(keys[-1][1])

    # бинарный поиск интервала
    lo, hi = 0, len(keys) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if keys[mid][0] <= frame:
            lo = mid
        else:
            hi = mid
    f0, v0 = keys[lo]
    f1, v1 = keys[hi]
    if mode == FRAME_INTERP_STEP or f1 == f0:
        return tuple(v0)
    t = (frame - f0) / float(f1 - f0)
    if is_rotation:
        return mat.quat_slerp(tuple(v0), tuple(v1), t)
    return tuple(a + (b - a) * t for a, b in zip(v0, v1))


def _bind_root_index(skeleton: Skeleton, root_joints) -> int:
    """Индекс кости, которая тащит тело: первая в корневой цепочке после корня."""
    return 1 if len(root_joints) > 1 else (min(root_joints) if root_joints else -1)


def _track_ref(track, options: BakeOptions):
    """Опорное значение кривой источника (её собственный rest)."""
    if options.motion_ref == MOTION_REF_MEDIAN:
        out = []
        for axis in range(3):
            values = sorted(v[axis] for _f, v in track.positions)
            out.append(values[len(values) // 2])
        return tuple(out)
    best = track.positions[0][1]
    for frame, value in track.positions:
        if frame <= options.motion_ref_frame:
            best = value
        else:
            break
    return tuple(best)


def _prepare_motion(anim: ClipAnim, skeleton: Skeleton, options: BakeOptions) -> dict:
    """Готовит перенос «только движения»: опорные значения, единицы, множитель."""
    root_joints = skeleton.root_chain()
    bind_index = _bind_root_index(skeleton, root_joints)
    rest_offsets = skeleton.offsets()
    hashes = skeleton.hash_map()

    # единица ЦЕЛЕВОГО рига — из его rest-данных
    unit_target = options.motion_unit_target
    if unit_target <= 0.0 and bind_index >= 0:
        unit_target = _vec_len(mat.to_translation(rest_offsets[bind_index]))
    # единица ИСТОЧНИКА — медиана длины трансляции bind-корня по клипу
    unit_source = options.motion_unit_source
    if unit_source <= 0.0:
        lengths = []
        for key, track in anim.tracks.items():
            index = hashes.get(key)
            if index != bind_index or not track.positions:
                continue
            # 90-й процентиль: «рост в стойке», устойчив к приседаниям и шуму
            values = sorted(_vec_len(v) for _f, v in track.positions)
            lengths.append(values[int(0.9 * (len(values) - 1))])
        if not lengths:
            # нет кривой bind-корня — берём самую «длинную» трансляцию в клипе
            for track in anim.tracks.values():
                if track.positions:
                    values = sorted(_vec_len(v) for _f, v in track.positions)
                    lengths.append(values[int(0.9 * (len(values) - 1))])
                    break
        unit_source = lengths[0] if lengths else 1.0

    factor = (unit_target / unit_source if unit_source > 1e-6 else 1.0) * options.motion_scale

    # локальная «вертикаль» в пространстве родителя bind-корня
    up = (0.0, 1.0, 0.0)
    if bind_index >= 0:
        rest = mat.to_translation(rest_offsets[bind_index])
        length = _vec_len(rest)
        if length > 1e-6:
            up = (rest[0] / length, rest[1] / length, rest[2] / length)

    refs = {}
    moving = set()
    for key, track in anim.tracks.items():
        index = hashes.get(key)
        if index is None:
            continue
        if not track.positions:
            continue
        is_root = index in root_joints
        if not is_root and not options.motion_service:
            from .diagnose import is_service_bone

            if is_service_bone(skeleton.joints[index].name):
                continue
        ref = _track_ref(track, options)
        refs[index] = ref
        for _f, value in track.positions:
            if max(abs(value[i] - ref[i]) for i in range(3)) > 1e-4:
                moving.add(index)
                break

    return {
        "factor": factor,
        "unit_source": unit_source,
        "unit_target": unit_target,
        "up": up,
        "refs": refs,
        "moving": moving,
        "root_joints": set(root_joints),
        "bind_index": bind_index,
    }


def _median(values):
    if not values:
        return 0.0
    values = sorted(values)
    return values[len(values) // 2]


def _vec_len(v) -> float:
    return (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5


def _apply_motion_delta(delta, prep, is_root, options: BakeOptions):
    """Масштабирует дельту источника и (для корня) демпфирует горизонталь."""
    factor = prep["factor"]
    out = [c * factor for c in delta]
    if is_root and options.motion_horizontal != 1.0:
        up = prep["up"]
        vertical = sum(out[i] * up[i] for i in range(3))
        for i in range(3):
            out[i] = vertical * up[i] + (out[i] - vertical * up[i]) * options.motion_horizontal
    return out


def motion_summary(anim: ClipAnim, skeleton: Skeleton, options: BakeOptions) -> dict:
    """Публичная обёртка: что аддон собирается сделать в режиме «только движение»."""
    prep = _prepare_motion(anim, skeleton, options)
    return {
        "unit_source": prep["unit_source"],
        "unit_target": prep["unit_target"],
        "factor": prep["factor"],
        "moving": [skeleton.joints[i].name for i in sorted(prep["moving"])],
    }


def _root_joint_set(skeleton: Skeleton, options: BakeOptions) -> set:
    """Кости, которым в режиме POSITIONS_ROOT разрешено двигаться."""
    name = (options.positions_root_bone or "").strip()
    if name:
        index = skeleton.joint_by_name(name)
        if index >= 0:
            allowed = {index}
            parent = skeleton.joints[index].parent
            while parent >= 0:
                allowed.add(parent)
                parent = skeleton.joints[parent].parent
            return allowed
    return set(skeleton.root_chain())


def _use_position_curve(index: int, shared: dict, options: BakeOptions) -> bool:
    """Применять ли к этой кости кривую позиции из клипа."""
    if options.positions == POSITIONS_ALL:
        return True
    if options.positions == POSITIONS_NONE:
        return False
    if options.positions == POSITIONS_MOTION:
        prep = shared.get("motion")
        return bool(prep) and index in prep["moving"]
    allowed = shared.get("root_joints")
    return index in allowed if allowed is not None else False


def bake_clip(anim: ClipAnim, skeleton: Skeleton, options: Optional[BakeOptions] = None) -> BakeResult:
    options = options or BakeOptions()
    hashes = skeleton.hash_map()
    offsets = skeleton.offsets()

    track_of: Dict[int, ClipTrack] = {}
    unmatched: List[int] = []
    morphs: List[ClipTrack] = []
    for key, track in anim.tracks.items():
        if track.is_morph_only:
            morphs.append(track)
            continue
        idx = hashes.get(key)
        if idx is None:
            unmatched.append(key)
        else:
            track_of[idx] = track

    shared = {"root_joints": None, "motion": None}
    if options.positions == POSITIONS_ROOT:
        shared["root_joints"] = _root_joint_set(skeleton, options)
    elif options.positions == POSITIONS_MOTION:
        shared["motion"] = _prepare_motion(anim, skeleton, options)

    frame_start = max(0, options.frame_start)
    frame_end = options.frame_end if options.frame_end is not None else anim.frame_end
    frame_end = max(frame_start, frame_end)

    if options.dense:
        frames: List[int] = list(range(frame_start, frame_end + 1))
    else:
        used = {frame_start}
        for track in track_of.values():
            used.update(f for f, _v in track.positions)
            used.update(f for f, _v in track.orientations)
        frames = sorted(used)

    curves: Dict[int, BoneCurves] = {}
    for idx, track in track_of.items():
        curves[idx] = BoneCurves(index=idx, name=skeleton.joints[idx].name)

    # считаем только кости, у которых есть данные: локальная база кости
    # не зависит от родителей, поэтому весь скелет перебирать не нужно
    tracked = sorted(track_of.items())

    for frame in frames:
        for idx, track in tracked:
            joint = skeleton.joints[idx]
            offsets_idx = offsets[idx]
            use_position = bool(track.positions) and _use_position_curve(idx, shared, options)
            pos = _sample(track.positions, frame, options.interpolate, False) if use_position else None
            prep = shared["motion"]
            if pos is not None and prep is not None:
                ref = prep["refs"].get(idx)
                if ref is not None:
                    delta = _apply_motion_delta(
                        [pos[i] - ref[i] for i in range(3)], prep, idx in prep["root_joints"], options
                    )
                    base_pos = mat.to_translation(offsets_idx)
                    pos = tuple(base_pos[i] + delta[i] for i in range(3))
            rot = _sample(track.orientations, frame, options.interpolate, True)

            if pos is not None:
                if options.position_mode == "delta" and shared["motion"] is None:
                    base = mat.to_translation(offsets_idx)
                    pos = (pos[0] + base[0], pos[1] + base[1], pos[2] + base[2])
                if options.scale != 1.0:
                    pos = (pos[0] * options.scale, pos[1] * options.scale, pos[2] * options.scale)
                t_mat = mat.translation(pos)
            elif options.missing_position == "absolute":
                t_mat = mat.translation(mat.to_translation(joint.matrix_local))
            else:  # 'relative' — кость остаётся в своей rest-позиции относительно родителя
                t_mat = mat.translation(mat.to_translation(offsets_idx))

            if rot is not None:
                if joint.parent < 0 and options.root_rotation == "identity":
                    r_mat = IDENTITY  # поведение оригинального плагина
                else:
                    if options.rotation_mode == "delta":
                        rot = mat.quat_mul(mat.to_quat(offsets_idx), rot)
                    r_mat = mat.from_quat(rot)
            else:
                r_mat = mat.rest_rotation(offsets_idx)

            target = mat.mul(t_mat, r_mat)
            basis = mat.mul(mat.inverse_rigid(offsets_idx), target)

            entry = curves[idx]
            if use_position:
                entry.positions.append((frame, mat.to_translation(basis)))
            if track.orientations:
                entry.rotations.append((frame, mat.to_quat(basis)))

    warnings = list(anim.warnings)
    if unmatched:
        warnings.append(
            "%d трек(ов) не сопоставлено с костями скелета (нужны имена джойнтов игры)"
            % len(unmatched)
        )
    if morphs:
        warnings.append("%d морф-трек(ов) пропущено (в FBX не переносятся)" % len(morphs))

    return BakeResult(
        frame_start=frame_start,
        frame_end=frame_end,
        bones=sorted(curves.values(), key=lambda b: b.index),
        unmatched_hashes=sorted(unmatched),
        morph_tracks=morphs,
        fps=anim.fps,
        warnings=warnings,
    )


def guess_joint_names(skeleton: Skeleton, hashes: Sequence[int]) -> Dict[int, str]:
    """Пытается найти имя кости по «похожему» хешу — для диагностики.

    Полезно, когда клип снят с другой модели: показываем, на что похож
    неопознанный ключ, сравнивая с хешами костей скелета без учёта регистра
    и с обрезанными служебными префиксами.
    """
    variants = {}
    for joint in skeleton.joints:
        name = joint.name
        variants[fnv1_32(name)] = name
        if name.startswith("b__") and name.endswith("__"):
            short = name[3:-2]
            variants.setdefault(fnv1_32(short), name)
    return {h: variants[h] for h in hashes if h in variants}
