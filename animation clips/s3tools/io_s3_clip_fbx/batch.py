"""Пакетная конвертация .animation → .fbx (или .glb для Godot)."""

from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import bpy

from . import blender as bl
from .s3clip import bake as s3bake
from .s3clip.clipfile import ClipReadError, read_clip, read_clip_variants

DEFAULT_EXTENSIONS = (".animation", ".clip", ".s3clip")

MODE_SEPARATE = "separate"  # один FBX на клип
MODE_SINGLE = "single"  # все клипы в один FBX (NLA-треки = отдельные анимации)


@dataclass
class BatchJob:
    out_dir: str = ""
    mode: str = MODE_SEPARATE
    format: str = "fbx"  # "fbx" или "gltf" (glTF 2.0 / .glb — формат для Godot)
    optimize_anim: bool = True  # выбрасывать неизменяющиеся каналы (для glTF)
    recursive: bool = True
    name_template: str = "{name}"
    single_file_name: str = "s3_animations.fbx"
    extensions: tuple = DEFAULT_EXTENSIONS
    skip_existing: bool = False

    # параметры бейка
    dense: bool = True
    interpolation: str = "LINEAR"
    missing_position: str = "relative"
    positions: str = "all"
    positions_root_bone: str = ""
    motion_ref: str = "frame"
    motion_ref_frame: int = 0
    motion_unit_source: float = 0.0
    motion_unit_target: float = 0.0
    motion_scale: float = 1.0
    motion_horizontal: float = 1.0
    motion_service: bool = False
    ground_snap: bool = True
    position_mode: str = "absolute"
    rotation_mode: str = "absolute"
    auto_convention: bool = True
    frame_padding: int = 0
    scale: float = 1.0

    # параметры экспорта
    include_meshes: bool = False
    add_leaf_bones: bool = False
    all_bones: bool = True
    deform_bones_only: bool = False
    axis_forward: str = "-Z"
    axis_up: str = "Y"
    keep_actions: bool = False


@dataclass
class FileResult:
    path: str
    ok: bool
    output: str = ""
    clip_name: str = ""
    frames: int = 0
    bones: int = 0
    unmatched: int = 0
    error: str = ""
    duration: float = 0.0


@dataclass
class BatchSummary:
    results: List[FileResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    output: str = ""
    elapsed: float = 0.0

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> List[FileResult]:
        return [r for r in self.results if not r.ok]


# --------------------------------------------------------------------------
def collect_files(paths, recursive: bool = True, extensions=DEFAULT_EXTENSIONS) -> List[str]:
    """Собирает список .animation из файлов/папок."""
    exts = tuple(e.lower() for e in extensions)
    found: List[str] = []
    for path in paths:
        if not path:
            continue
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isfile(path):
            if path.lower().endswith(exts):
                found.append(path)
            continue
        if not os.path.isdir(path):
            continue
        if recursive:
            for root, _dirs, files in os.walk(path):
                for name in sorted(files):
                    if name.lower().endswith(exts):
                        found.append(os.path.join(root, name))
        else:
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if os.path.isfile(full) and name.lower().endswith(exts):
                    found.append(full)
    # убираем дубликаты, сохраняя порядок
    seen = set()
    unique = []
    for item in found:
        key = os.path.normcase(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _progress_begin(total: int, label: str):
    try:
        bpy.context.window_manager.progress_begin(0, max(1, total))
    except Exception:
        pass


def _progress_update(step: int, label: str = ""):
    try:
        bpy.context.window_manager.progress_update(step)
    except Exception:
        pass


def _progress_end():
    try:
        bpy.context.window_manager.progress_end()
    except Exception:
        pass


def _clear_nla(arm_obj):
    ad = arm_obj.animation_data
    if ad is None:
        return
    for track in list(ad.nla_tracks):
        ad.nla_tracks.remove(track)


def _bake_options(job: BatchJob, frame_end: Optional[int] = None) -> s3bake.BakeOptions:
    return s3bake.BakeOptions(
        dense=job.dense,
        interpolate="linear" if job.interpolation.upper() != "CONSTANT" else "step",
        frame_start=0,
        frame_end=frame_end,
        missing_position=job.missing_position,
        positions=job.positions,
        positions_root_bone=(job.positions_root_bone or None),
        motion_ref=job.motion_ref,
        motion_ref_frame=job.motion_ref_frame,
        motion_unit_source=job.motion_unit_source,
        motion_unit_target=job.motion_unit_target,
        motion_scale=job.motion_scale,
        motion_horizontal=job.motion_horizontal,
        motion_service=job.motion_service,
        ground_snap=job.ground_snap,
        position_mode=job.position_mode,
        rotation_mode=job.rotation_mode,
        auto_convention=job.auto_convention,
        scale=job.scale,
    )


def run_batch(
    context,
    arm_obj: bpy.types.Object,
    files: List[str],
    job: BatchJob,
    log: Callable[[str], None] = print,
    progress: Callable[[int, int, str], None] = None,
) -> BatchSummary:
    started = time.time()
    summary = BatchSummary()
    os.makedirs(job.out_dir, exist_ok=True)

    bl.ensure_fbx_addon()
    bl.set_quaternion_mode(arm_obj)

    scene = context.scene
    previous_fps = scene.render.fps
    previous_fps_base = scene.render.fps_base
    ad = arm_obj.animation_data
    previous_action = ad.action if ad is not None else None
    if previous_action is not None:
        previous_action.use_fake_user = True  # чтобы не пропал при удалении экшенов в батче

    _progress_begin(len(files), "Sims 3 → FBX")
    try:
        if job.mode == MODE_SINGLE:
            _run_single(context, arm_obj, files, job, summary, log, progress)
        else:
            _run_separate(context, arm_obj, files, job, summary, log, progress)
    finally:
        scene.render.fps = previous_fps
        scene.render.fps_base = previous_fps_base
        if previous_action is not None:
            try:
                ad = arm_obj.animation_data or arm_obj.animation_data_create()
                ad.action = previous_action
                previous_action.use_fake_user = False
            except Exception:
                pass
        _progress_end()

    summary.elapsed = time.time() - started
    return summary


def _set_fps(scene, fps: float):
    fps_int = int(round(fps)) or 30
    scene.render.fps = fps_int
    scene.render.fps_base = 1.0


def _run_separate(context, arm_obj, files, job, summary, log, progress):
    scene = context.scene
    total = len(files)
    used_names = set()
    for index, path in enumerate(files):
        label = os.path.basename(path)
        if progress:
            progress(index, total, label)
        _progress_update(index)
        started = time.time()
        entry = FileResult(path=path, ok=False)
        try:
            variants = read_clip_variants(path)
            anim = next(iter(variants.values()))
            entry.clip_name = anim.name
            ext = ".glb" if job.format == "gltf" else ".fbx"
            out_name = bl.output_name(job.name_template, anim, path, index, ext=ext)
            base, ext = os.path.splitext(out_name)
            suffix = 2
            while out_name in used_names:  # два клипа с одинаковым именем — не затираем
                out_name = "%s_%d%s" % (base, suffix, ext)
                suffix += 1
            used_names.add(out_name)
            out_path = os.path.join(job.out_dir, out_name)
            entry.output = out_path
            if job.skip_existing and os.path.exists(out_path):
                entry.ok = True
                log("  пропуск (файл уже есть): %s" % out_name)
                continue

            _clear_nla(arm_obj)
            _set_fps(scene, anim.fps)
            action, result = bl.build_action(
                arm_obj,
                anim,
                _bake_options(job, anim.frame_end + job.frame_padding),
                interpolation=job.interpolation,
                variants=variants,
            )
            scene.frame_start = result.frame_start
            scene.frame_end = result.frame_end
            entry.frames = result.frame_end - result.frame_start + 1
            entry.bones = result.matched_bones
            entry.unmatched = len(result.unmatched_hashes)
            for warning in result.warnings:
                summary.warnings.append("%s: %s" % (label, warning))

            if job.format == "gltf":
                bl.export_gltf(
                    context,
                    out_path,
                    arm_obj,
                    include_meshes=job.include_meshes,
                    optimize=job.optimize_anim,
                )
            else:
                bl.export_fbx(
                    context,
                    out_path,
                    arm_obj,
                    include_meshes=job.include_meshes,
                    add_leaf_bones=job.add_leaf_bones,
                    axis_forward=job.axis_forward,
                    axis_up=job.axis_up,
                    all_bones=job.all_bones,
                    deform_bones_only=job.deform_bones_only,
                )
            entry.ok = True
            log(
                "  OK  %-40s кадров: %3d  костей: %3d  %s"
                % (label[:40], entry.frames, entry.bones, out_name)
            )
            if entry.unmatched:
                log("      ! %d трек(ов) без пары в скелете" % entry.unmatched)
        except ClipReadError as exc:
            entry.error = "формат: %s" % exc
            log("  FAIL %s — %s" % (label, entry.error))
        except Exception as exc:  # noqa: BLE001
            entry.error = "%s: %s" % (type(exc).__name__, exc)
            log("  FAIL %s — %s" % (label, entry.error))
            if log is print:
                traceback.print_exc()
        finally:
            entry.duration = time.time() - started
            if not job.keep_actions:
                ad = arm_obj.animation_data
                action = ad.action if ad else None
                if ad is not None:
                    ad.action = None
                if action is not None and action.users == 0:
                    bpy.data.actions.remove(action)
            summary.results.append(entry)
    if progress:
        progress(total, total, "")


def _run_single(context, arm_obj, files, job, summary, log, progress):
    scene = context.scene
    total = len(files)
    ad = arm_obj.animation_data or arm_obj.animation_data_create()
    ad.action = None
    _clear_nla(arm_obj)
    fps = 30.0
    created = []
    used_takes = set()

    for index, path in enumerate(files):
        label = os.path.basename(path)
        if progress:
            progress(index, total, label)
        _progress_update(index)
        started = time.time()
        entry = FileResult(path=path, ok=False)
        try:
            variants = read_clip_variants(path)
            anim = next(iter(variants.values()))
            entry.clip_name = anim.name
            fps = anim.fps
            _set_fps(scene, anim.fps)
            take = anim.name or os.path.splitext(os.path.basename(path))[0]
            base_take = take
            counter = 2
            while take in used_takes:
                take = "%s_%d" % (base_take, counter)
                counter += 1
            used_takes.add(take)
            action, result = bl.build_action(
                arm_obj,
                anim,
                _bake_options(job, anim.frame_end + job.frame_padding),
                action_name=take,
                interpolation=job.interpolation,
                variants=variants,
            )
            entry.frames = result.frame_end - result.frame_start + 1
            entry.bones = result.matched_bones
            entry.unmatched = len(result.unmatched_hashes)
            ad = arm_obj.animation_data or arm_obj.animation_data_create()
            ad.action = None
            track = ad.nla_tracks.new()
            track.name = take
            strip = track.strips.new(take, int(result.frame_start), action)
            strip.name = take
            # Blender 4.4+: у экшена есть слоты, у полосы NLA — свой action_slot.
            # Обычно Blender подставляет первый слот сам, но на всякий случай проверяем.
            if hasattr(strip, "action_slot") and strip.action_slot is None:
                slots = getattr(action, "slots", None)
                if slots:
                    try:
                        strip.action_slot = slots[0]
                    except Exception:  # noqa: BLE001
                        pass
            created.append(action)
            entry.ok = True
            log("  +   %-40s кадров: %3d  костей: %3d  take: %s" % (label[:40], entry.frames, entry.bones, take))
        except Exception as exc:  # noqa: BLE001
            entry.error = "%s: %s" % (type(exc).__name__, exc)
            log("  FAIL %s — %s" % (label, entry.error))
        finally:
            entry.duration = time.time() - started
            summary.results.append(entry)

    if not created:
        summary.warnings.append("нечего экспортировать: ни один клип не прочитан")
        if progress:
            progress(total, total, "")
        return

    _set_fps(scene, fps)
    strips = [strip for track in arm_obj.animation_data.nla_tracks for strip in track.strips]
    if strips:
        scene.frame_start = int(min(s.frame_start for s in strips))
        scene.frame_end = int(max(s.frame_end for s in strips))
    single_ext = ".glb" if job.format == "gltf" else ".fbx"
    default_name = "s3_animations" + single_ext
    single_name = job.single_file_name
    if job.format == "gltf" and single_name.lower().endswith(".fbx"):
        single_name = single_name[:-4] + ".glb"
    out_path = os.path.join(job.out_dir, bl.sanitize_filename(single_name, default_name))
    if not out_path.lower().endswith(single_ext):
        out_path += single_ext
    summary.output = out_path
    kind = "glTF" if job.format == "gltf" else "FBX"
    try:
        if job.format == "gltf":
            bl.export_gltf(
                context,
                out_path,
                arm_obj,
                include_meshes=job.include_meshes,
                nla_strips=True,
                optimize=job.optimize_anim,
            )
        else:
            bl.export_fbx(
                context,
                out_path,
                arm_obj,
                include_meshes=job.include_meshes,
                add_leaf_bones=job.add_leaf_bones,
                axis_forward=job.axis_forward,
                axis_up=job.axis_up,
                nla_strips=True,
                all_bones=job.all_bones,
                deform_bones_only=job.deform_bones_only,
            )
        for entry in summary.results:
            if entry.ok:
                entry.output = out_path
        log("  OK  один %s со всеми клипами: %s" % (kind, out_path))
    except Exception as exc:  # noqa: BLE001
        summary.warnings.append("экспорт общего %s не удался: %s" % (kind, exc))
        log("  FAIL общий %s — %s" % (kind, exc))
    finally:
        if not job.keep_actions:
            _clear_nla(arm_obj)
            for action in created:
                if action.users == 0:
                    bpy.data.actions.remove(action)
    if progress:
        progress(total, total, "")
