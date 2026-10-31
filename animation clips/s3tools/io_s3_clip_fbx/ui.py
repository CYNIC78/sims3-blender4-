"""UI: панель в N-сайдбаре и операторы."""

from __future__ import annotations

import os

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy_extras.io_utils import ImportHelper

from . import batch as batch_mod
from . import blender as bl
from .s3clip import bake as s3bake
from .s3clip import diagnose as s3diag
from .s3clip.clipfile import ClipReadError, read_clip, read_clip_variants
from .s3clip.fnv import fnv1_32

LOG_LINES = []


def _log(message: str):
    LOG_LINES.append(message)
    print("[S3 Clip→FBX] %s" % message)


def _drain_log(max_lines=200) -> str:
    if not LOG_LINES:
        return ""
    text = "\n".join(LOG_LINES[-max_lines:])
    LOG_LINES.clear()
    return text


def _write_log_block(text: str):
    if not text:
        return
    name = "S3 Clip→FBX log"
    block = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    block.clear()
    block.write(text + "\n")


def _armature_poll(self, obj):
    return obj.type == "ARMATURE"


class S3ClipFbxSettings(bpy.types.PropertyGroup):
    armature: PointerProperty(
        name="Арматура",
        description="Риг Sims 3. Пусто — берётся активный объект",
        type=bpy.types.Object,
        poll=_armature_poll,
    )
    input_dir: StringProperty(
        name="Папка с клипами",
        description="Откуда брать .animation",
        subtype="DIR_PATH",
        default="",
    )
    output_dir: StringProperty(
        name="Папка для FBX",
        subtype="DIR_PATH",
        default="",
    )
    recursive: BoolProperty(name="Включая подпапки", default=True)
    mode: EnumProperty(
        name="Режим",
        items=(
            ("separate", "Отдельный FBX на клип", "Каждый .animation → свой .fbx"),
            ("single", "Один FBX со всеми клипами", "Все клипы как NLA-треки (takes) в одном файле"),
        ),
        default="separate",
    )
    format: EnumProperty(
        name="Формат",
        items=(
            ("fbx", "FBX", "Для Unity, Blender, Unreal"),
            ("gltf", "glTF (.glb)", "Для Godot: один файл может содержать все клипы"),
        ),
        default="fbx",
    )
    name_template: StringProperty(
        name="Шаблон имени",
        description="Доступно: {name} {stem} {index} {actor}",
        default="{name}",
    )
    single_file_name: StringProperty(name="Имя общего FBX", default="s3_animations.fbx")
    chunk_size: IntProperty(
        name="Клипов на файл",
        description="Только glTF в режиме «один файл»: бить библиотеку на файлы по N клипов "
                    "(имена получат суффикс _01, _02 …). 0 — всё в один файл",
        default=0,
        min=0,
        soft_max=500,
    )
    skip_existing: BoolProperty(name="Пропускать уже готовые", default=True)

    dense: BoolProperty(
        name="Ключ на каждом кадре",
        description="Плотный бейк: каждая кость ключуется каждый кадр (надёжнее для FBX/движков)",
        default=True,
    )
    interpolation: EnumProperty(
        name="Интерполяция",
        items=(
            ("LINEAR", "Линейная", "Как в игре: линейно между «родными» кадрами"),
            ("CONSTANT", "Ступеньками", "Держать значение до следующего родного кадра"),
            ("BEZIER", "Безье", "Сглаженная (может «перелетать» позы)"),
        ),
        default="LINEAR",
    )
    missing_position: EnumProperty(
        name="Нет данных позиции",
        description="Что делать костям, у которых в клипе нет кривой позиции",
        items=(
            ("relative", "Оставить в rest-позиции", "Кость остаётся на своём месте относительно родителя"),
            ("absolute", "Как в оригинальном плагине", "Берётся абсолютная rest-позиция кости"),
        ),
        default="relative",
    )
    positions: EnumProperty(
        name="Позиции костей",
        description="Какие кривые позиции применять. Для рига, у которого пропорции "
                    "не совпадают с игровым скелетом, лучше «только корневая кость»: "
                    "анимация останется чисто вращательной и кости не растянет",
        items=(
            ("motion", "Только движение (рекомендуется)",
             "Из клипа берётся движение относительно rest источника и пересчитывается "
             "в единицы вашего рига: работает на любом росте и пропорциях"),
            ("root", "Корневая цепочка целиком",
             "Абсолютные значения корня как в клипе: нужно, если риг совпадает по пропорциям"),
            ("all", "Все (как в клипе)", "Абсолютные позиции всех костей, точно как в игре"),
            ("none", "Не импортировать", "Только повороты, без всякой трансляции"),
        ),
        default="motion",
    )
    motion_ref: EnumProperty(
        name="Опорное значение",
        description="Что считать rest-позой источника: значение на кадре или медиану по клипу",
        items=(
            ("frame", "Кадр", "Значение кривой на опорном кадре (обычно клип начинается со стойки)"),
            ("median", "Медиана", "Медиана по всем кадрам — устойчивее к дрожанию, но врёт на «сидячих» клипах"),
        ),
        default="frame",
    )
    motion_ref_frame: IntProperty(name="Опорный кадр", default=0, min=0, max=10000)
    motion_unit_source: FloatProperty(
        name="Единица источника",
        description="Высота bind-корня скелета игры, м. 0 — определить по самому клипу",
        default=0.0, min=0.0, max=10.0, precision=4,
    )
    motion_unit_target: FloatProperty(
        name="Единица рига",
        description="Высота bind-корня ВАШЕГО рига в rest, м. 0 — взять из рига автоматически",
        default=0.0, min=0.0, max=10.0, precision=4,
    )
    motion_scale: FloatProperty(
        name="Множитель амплитуд", description="Тонкая подстройка всех перенесённых движений",
        default=1.0, min=0.0, max=5.0, precision=3,
    )
    motion_horizontal: FloatProperty(
        name="Горизонтальное покачивание",
        description="Сколько оставить от бокового/продольного смещения корня (1.0 — как в клипе)",
        default=1.0, min=0.0, max=1.5, precision=3,
    )
    motion_service: BoolProperty(
        name="Двигать служебные кости", description="Info / ExportPole / slotOffset — обычно не нужно",
        default=False,
    )
    ground_snap: BoolProperty(
        name="Прижать к полу",
        description="После бейка сдвинуть корень так, чтобы самая низкая точка стоп была на z=0",
        default=True,
    )
    ai_positions_root: StringProperty(
        name="Корневая кость",
        description="Если автоопределение корня не подходит: имя кости, чья трансляция должна "
                    "сохраниться (её предки тоже). Пусто — определить автоматически",
        default="",
    )
    auto_convention: BoolProperty(
        name="Определять конвенцию автоматически",
        description="Перебрать 4 варианта чтения (абсолютные/дельта) и выбрать тот, "
                    "при котором поза ближе к rest — защита от «риг сломался»",
        default=True,
    )
    position_mode: EnumProperty(
        name="Позиции",
        description="Как читать кривые позиции из клипа",
        items=(
            ("absolute", "Абсолютные (как в оригинале)", "Значение = смещение кости относительно родителя"),
            ("delta", "Дельта от rest", "Значение прибавляется к rest-смещению кости"),
        ),
        default="absolute",
    )
    rotation_mode: EnumProperty(
        name="Повороты",
        description="Как читать кватернионы из клипа",
        items=(
            ("absolute", "Абсолютные (как в оригинале)", "Кватернион = локальный поворот как есть"),
            ("delta", "Дельта от rest", "Кватернион домножается на rest-поворот кости"),
        ),
        default="absolute",
    )
    frame_padding: IntProperty(name="Кадров в конце", default=0, min=0, max=240)
    scale: FloatProperty(name="Масштаб позиций", default=1.0, min=0.0001, max=1000.0)

    include_meshes: BoolProperty(name="Включить меши", default=False)
    add_leaf_bones: BoolProperty(name="Листовые кости (leaf bones)", default=False)
    deform_bones_only: BoolProperty(
        name="Только деформирующие кости",
        description="Не выгружать служебные кости (_slot, Target, Info) — файл заметно меньше",
        default=False,
    )
    all_bones: BoolProperty(
        name="Все кости в FBX",
        description="Выгружать все кости рига, даже неанимированные (файл больше, зато скелет полный)",
        default=True,
    )
    axis_forward: EnumProperty(
        name="Forward",
        items=(("-Z", "-Z", ""), ("Z", "Z", ""), ("Y", "Y", ""), ("X", "X", ""), ("-Y", "-Y", "")),
        default="-Z",
    )
    axis_up: EnumProperty(
        name="Up",
        items=(("Y", "Y", ""), ("Z", "Z", ""), ("X", "X", "")),
        default="Y",
    )
    keep_actions: BoolProperty(
        name="Оставлять экшены в файле",
        description="Не удалять созданные экшены после экспорта",
        default=False,
    )


def _settings(context) -> S3ClipFbxSettings:
    return context.scene.s3clip_fbx


def _resolve_armature(context, settings):
    return bl.find_armature(context, settings.armature)


FIELD_ORDER_LABELS = {
    "offset_scale": "сначала offset, потом scale (как в спецификации)",
    "scale_offset": "сначала scale, потом offset (нестандартный файл)",
}


def _order_label(order: str) -> str:
    return "%s — %s" % (order, FIELD_ORDER_LABELS.get(order, "?"))


def _read_best(path, skeleton, settings=None, convention=None):
    """Читает клип, пробуя оба порядка полей в описании кривой.

    Возвращает (anim, field_order, (position_mode, rotation_mode)).
    """
    variants = read_clip_variants(path)
    if len(variants) == 1:
        anim = next(iter(variants.values()))
        return anim, anim.field_order, (None, None)
    order, pos_mode, rot_mode, scores = s3diag.detect_field_order(
        variants, skeleton, _bake_options(settings) if settings else None, convention
    )
    print(
        "[S3 Clip→FBX] порядок полей в кривых: %s (сдвиг от rest: %s)"
        % (order, {k: round(v, 2) for k, v in scores.items()})
    )
    return variants[order], order, (pos_mode, rot_mode)


def _bake_options(settings, frame_end=None) -> s3bake.BakeOptions:
    interp = (
        s3bake.FRAME_INTERP_STEP
        if settings.interpolation == "CONSTANT"
        else s3bake.FRAME_INTERP_LINEAR
    )
    return s3bake.BakeOptions(
        dense=settings.dense,
        interpolate=interp,
        frame_end=frame_end,
        missing_position=settings.missing_position,
        positions=settings.positions,
        positions_root_bone=(settings.ai_positions_root or None),
        motion_ref=settings.motion_ref,
        motion_ref_frame=settings.motion_ref_frame,
        motion_unit_source=settings.motion_unit_source,
        motion_unit_target=settings.motion_unit_target,
        motion_scale=settings.motion_scale,
        motion_horizontal=settings.motion_horizontal,
        motion_service=settings.motion_service,
        ground_snap=settings.ground_snap,
        position_mode=settings.position_mode,
        rotation_mode=settings.rotation_mode,
        auto_convention=settings.auto_convention,
        scale=settings.scale,
    )


def _make_job(settings) -> batch_mod.BatchJob:
    return batch_mod.BatchJob(
        out_dir=bpy.path.abspath(settings.output_dir),
        mode=settings.mode,
        format=settings.format,
        recursive=settings.recursive,
        name_template=settings.name_template,
        single_file_name=settings.single_file_name,
        skip_existing=settings.skip_existing,
        dense=settings.dense,
        interpolation=settings.interpolation,
        missing_position=settings.missing_position,
        positions=settings.positions,
        positions_root_bone=(settings.ai_positions_root or None),
        motion_ref=settings.motion_ref,
        motion_ref_frame=settings.motion_ref_frame,
        motion_unit_source=settings.motion_unit_source,
        motion_unit_target=settings.motion_unit_target,
        motion_scale=settings.motion_scale,
        motion_horizontal=settings.motion_horizontal,
        motion_service=settings.motion_service,
        ground_snap=settings.ground_snap,
        position_mode=settings.position_mode,
        rotation_mode=settings.rotation_mode,
        auto_convention=settings.auto_convention,
        frame_padding=settings.frame_padding,
        scale=settings.scale,
        include_meshes=settings.include_meshes,
        add_leaf_bones=settings.add_leaf_bones,
        all_bones=settings.all_bones,
        deform_bones_only=settings.deform_bones_only,
        axis_forward=settings.axis_forward,
        axis_up=settings.axis_up,
        keep_actions=settings.keep_actions,
    )


def _report_summary(op, summary: batch_mod.BatchSummary):
    _write_log_block(_drain_log())
    ok = summary.ok_count
    failed = len(summary.results) - ok
    msg = "Готово: %d ок" % ok
    if failed:
        msg += ", %d с ошибкой" % failed
    msg += " за %.1f с" % summary.elapsed
    level = {"INFO"} if not failed else {"WARNING"}
    op.report(level, msg)


def _run_batch_maybe_chunked(op, context, settings, arm, files) -> None:
    """Запустить пакет, при необходимости — кусками по chunk_size клипов.

    Библиотеку на тысячи клипов держим кусками: один .glb на N клипов.
    В Godot это N AnimationLibrary, которые складываются в один AnimationPlayer.
    """
    job = _make_job(settings)
    chunk = int(getattr(settings, "chunk_size", 0) or 0)
    if not (chunk > 0 and settings.mode == "single" and settings.format == "gltf"):
        summary = batch_mod.run_batch(context, arm, files, job, log=_log)
        _report_summary(op, summary)
        return

    base, ext = os.path.splitext(job.single_file_name)
    parts = [files[i:i + chunk] for i in range(0, len(files), chunk)]
    _log("библиотека: %d файл(ов) по %d клипов" % (len(parts), chunk))

    total_ok = 0
    total_failed = 0
    elapsed = 0.0
    for index, part in enumerate(parts, start=1):
        job.single_file_name = "%s_%02d%s" % (base, index, ext)
        _log("— кусок %d/%d: %d клипов → %s" % (index, len(parts), len(part), job.single_file_name))
        summary = batch_mod.run_batch(context, arm, part, job, log=_log)
        total_ok += summary.ok_count
        total_failed += len(summary.failed)
        elapsed += summary.elapsed

    _write_log_block(_drain_log())
    msg = "Готово: %d ок, файлов %d" % (total_ok, len(parts))
    if total_failed:
        msg += ", %d с ошибкой" % total_failed
    msg += " за %.1f с" % elapsed
    op.report({"INFO"} if not total_failed else {"WARNING"}, msg)


# --------------------------------------------------------------------------
# Операторы
# --------------------------------------------------------------------------
class S3CLIP_OT_import_clip(bpy.types.Operator, ImportHelper):
    """Импортировать .animation как экшен на активном риге"""

    bl_idname = "s3clipfbx.import_clip"
    bl_label = "Sims 3 Animation (.animation)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".animation"
    filter_glob: StringProperty(default="*.animation;*.clip;*.s3clip", options={"HIDDEN"})
    single: BoolProperty(
        name="Заменить существующий экшен",
        description="Снять текущий экшен с рига перед импортом",
        default=True,
    )

    def execute(self, context):
        settings = _settings(context)
        arm = _resolve_armature(context, settings)
        if arm is None:
            self.report({"ERROR"}, "Не найдена арматура: выберите риг Sims 3")
            return {"CANCELLED"}
        try:
            variants = read_clip_variants(self.filepath)
        except ClipReadError as exc:
            self.report({"ERROR"}, "Не CLIP: %s" % exc)
            return {"CANCELLED"}
        anim = next(iter(variants.values()))

        bl.set_quaternion_mode(arm)
        if self.single:
            bl.clear_animation(arm)
        action, result = bl.build_action(
            arm,
            anim,
            _bake_options(settings, anim.frame_end),
            interpolation=settings.interpolation,
            variants=variants,
        )
        context.scene.frame_start = result.frame_start
        context.scene.frame_end = result.frame_end
        context.scene.render.fps = int(round(anim.fps)) or 30

        for warning in result.warnings:
            _log("! %s" % warning)
        missing = s3bake.guess_joint_names(
            bl.skeleton_from_armature(arm), result.unmatched_hashes
        )
        for key in result.unmatched_hashes:
            _log("  нет кости для ключа %08X%s" % (key, " (похоже на %s)" % missing[key] if key in missing else ""))
        _write_log_block(_drain_log())

        self.report(
            {"INFO"},
            "%s: кадров %d, костей %d" % (action.name, result.frame_end + 1, result.matched_bones),
        )
        return {"FINISHED"}


class S3CLIP_OT_batch_dir(bpy.types.Operator):
    """Пакетно конвертировать все .animation из папки в FBX"""

    bl_idname = "s3clipfbx.batch_dir"
    bl_label = "Конвертировать папку в FBX"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = _settings(context)
        if not settings.input_dir:
            self.report({"ERROR"}, "Укажите папку с клипами")
            return {"CANCELLED"}
        if not settings.output_dir:
            self.report({"ERROR"}, "Укажите папку для FBX")
            return {"CANCELLED"}
        arm = _resolve_armature(context, settings)
        if arm is None:
            self.report({"ERROR"}, "Не найдена арматура: выберите риг Sims 3")
            return {"CANCELLED"}

        files = batch_mod.collect_files(
            [bpy.path.abspath(settings.input_dir)],
            settings.recursive,
            batch_mod.DEFAULT_EXTENSIONS,
        )
        if not files:
            self.report({"WARNING"}, "В папке нет .animation")
            return {"CANCELLED"}

        _log("файлов найдено: %d" % len(files))
        _run_batch_maybe_chunked(self, context, settings, arm, files)
        return {"FINISHED"}


class S3CLIP_OT_batch_files(bpy.types.Operator, ImportHelper):
    """Выбрать .animation файлы и конвертировать их в FBX"""

    bl_idname = "s3clipfbx.batch_files"
    bl_label = "Конвертировать выбранные клипы в FBX"
    bl_options = {"REGISTER", "UNDO"}

    filter_glob: StringProperty(default="*.animation;*.clip;*.s3clip", options={"HIDDEN"})
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        settings = _settings(context)
        if not settings.output_dir:
            self.report({"ERROR"}, "Укажите папку для FBX")
            return {"CANCELLED"}
        arm = _resolve_armature(context, settings)
        if arm is None:
            self.report({"ERROR"}, "Не найдена арматура: выберите риг Sims 3")
            return {"CANCELLED"}

        paths = [os.path.join(self.directory, f.name) for f in self.files if f.name]
        if not paths:
            paths = [self.filepath] if getattr(self, "filepath", "") else []
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            self.report({"WARNING"}, "Файлы не выбраны")
            return {"CANCELLED"}

        summary = None
        chunk = int(getattr(settings, "chunk_size", 0) or 0)
        if chunk > 0 and settings.mode == "single" and settings.format == "gltf":
            _run_batch_maybe_chunked(self, context, settings, arm, [p for p in paths])
            return {"FINISHED"}
        summary = batch_mod.run_batch(context, arm, paths, _make_job(settings), log=_log)
        _report_summary(self, summary)
        return {"FINISHED"}


class S3CLIP_OT_dump_rig_map(bpy.types.Operator):
    """Вывести в консоль таблицу «кость → FNV-хеш» (для сверки с клипом)"""

    bl_idname = "s3clipfbx.dump_rig_map"
    bl_label = "Показать хеши костей"

    def execute(self, context):
        settings = _settings(context)
        arm = _resolve_armature(context, settings)
        if arm is None:
            self.report({"ERROR"}, "Не найдена арматура")
            return {"CANCELLED"}
        lines = []
        for bone in arm.data.bones:
            parent = bone.parent.name if bone.parent else "-"
            lines.append("%08X  %-40s parent: %s" % (fnv1_32(bone.name), bone.name, parent))
        print("\n".join(lines))
        _log("таблица хешей костей (%d) выведена в System Console" % len(lines))
        _write_log_block("\n".join(lines))
        self.report({"INFO"}, "Таблица в консоли и в тексте «S3 Clip→FBX log»")
        return {"FINISHED"}


class S3CLIP_OT_check_clips(bpy.types.Operator, ImportHelper):
    """Проверить клип(ы) и показать, какие кости не сопоставились"""

    bl_idname = "s3clipfbx.check_clips"
    bl_label = "Проверить клип без экспорта"
    bl_options = {"REGISTER"}

    filter_glob: StringProperty(default="*.animation;*.clip;*.s3clip", options={"HIDDEN"})
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        settings = _settings(context)
        arm = _resolve_armature(context, settings)
        if arm is None:
            self.report({"ERROR"}, "Не найдена арматура")
            return {"CANCELLED"}
        paths = [os.path.join(self.directory, f.name) for f in self.files if f.name]
        if not paths and getattr(self, "filepath", ""):
            paths = [self.filepath]

        skeleton = bl.skeleton_from_armature(arm)
        lines = []
        for path in paths:
            try:
                anim, order, _modes = _read_best(path, skeleton, settings)
            except Exception as exc:  # noqa: BLE001
                lines.append("%s: ОШИБКА %s" % (os.path.basename(path), exc))
                continue
            result = s3bake.bake_clip(anim, skeleton, _bake_options(settings))
            lines.append(
                "%s | %s | имя: %s | актор: %s | %d..%d кадров @ %.1f fps | костей: %d | без пары: %d"
                % (
                    os.path.basename(path),
                    _order_label(order),
                    anim.name,
                    anim.actor_name,
                    result.frame_start,
                    result.frame_end,
                    anim.fps,
                    result.matched_bones,
                    len(result.unmatched_hashes),
                )
            )
            missing = s3bake.guess_joint_names(skeleton, result.unmatched_hashes)
            for key in result.unmatched_hashes[:20]:
                lines.append(
                    "    %08X%s" % (key, "  ≈ %s" % missing[key] if key in missing else "")
                )
            for warning in result.warnings:
                lines.append("    ! %s" % warning)
        print("\n".join(lines))
        _write_log_block("\n".join(lines))
        self.report({"INFO"}, "Отчёт в консоли и в тексте «S3 Clip→FBX log»")
        return {"FINISHED"}


class S3CLIP_OT_analyze_clip(bpy.types.Operator, ImportHelper):
    """Проверить клип: конвенция позиций/поворотов и кто «уезжает» от rest"""

    bl_idname = "s3clipfbx.analyze_clip"
    bl_label = "Диагностика клипа"
    bl_options = {"REGISTER"}

    filter_glob: StringProperty(default="*.animation;*.clip;*.s3clip", options={"HIDDEN"})
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        settings = _settings(context)
        arm = _resolve_armature(context, settings)
        if arm is None:
            self.report({"ERROR"}, "Не найдена арматура")
            return {"CANCELLED"}
        paths = [os.path.join(self.directory, f.name) for f in self.files if f.name]
        if not paths and getattr(self, "filepath", ""):
            paths = [self.filepath]

        skeleton = bl.skeleton_from_armature(arm)
        lines = []
        for path in paths:
            try:
                anim, order, _modes = _read_best(path, skeleton, settings)
            except Exception as exc:  # noqa: BLE001
                lines.append("%s: ОШИБКА %s" % (os.path.basename(path), exc))
                continue
            a = s3diag.analyze_clip(anim, skeleton)
            lines.append("=== %s" % os.path.basename(path))
            lines.append("  порядок полей в кривых: %s" % _order_label(order))
            lines.append(
                "  имя: %s | %.0f fps | кадров объявлено %d, реально %d"
                % (a.name, a.fps, a.declared_frames, a.actual_frames)
            )
            lines.append(
                "  треков: %d (сопоставлено %d, с позициями %d, морфов %d)"
                % (a.tracks_total, a.tracks_matched, a.tracks_with_position, a.morph_tracks)
            )
            for verdict in a.verdict:
                lines.append("  → %s" % verdict)
            lines.append("  крупнейшие треки (кость: позиция клипа / rest, поворот клипа / rest):")
            for stat in a.sample:
                lines.append(
                    "    %-26s %7.3f / %7.3f м   %6.1f° / %6.1f°"
                    % (stat.bone, stat.clip_pos_len, stat.rest_pos_len,
                       stat.clip_rot_deg, stat.rest_rot_deg)
                )
            offenders = s3diag.fk_check(
                anim, skeleton, _bake_options(settings, min(anim.frame_end, 4))
            )
            lines.append("  кто уехал от rest сильнее всех (первые 5 кадров):")
            for off in offenders[:8]:
                lines.append(
                    "    %-26s сдвиг %6.2f м  (длина кости %.3f м) кадр %d%s"
                    % (off.bone,
                       off.max_shift,
                       off.rest_len,
                       off.frame,
                       "   [служебная кость рига — это нормально]" if off.service else "")
                )
            for warning in a.warnings[:10]:
                lines.append("  ! %s" % warning)
        text = "\n".join(lines)
        print(text)
        _write_log_block(text)
        self.report({"INFO"}, "Отчёт в консоли и в тексте «S3 Clip→FBX log»")
        return {"FINISHED"}


class S3CLIP_OT_mirror_names(bpy.types.Operator):
    """Переключить имена костей: b__L_UpperArm__ ⇄ UpperArm.L (для симметрии весов)"""

    bl_idname = "s3clipfbx.mirror_names"
    bl_label = "Имена костей: Sims 3 ⇄ .L/.R"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = _settings(context)
        arm = _resolve_armature(context, settings)
        if arm is None:
            self.report({"ERROR"}, "Не найдена арматура")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass

        to_mirror = not bl.has_mirror_names(arm)
        info = bl.rename_bones_side_mode(arm, to_mirror)
        if not info["bones"]:
            self.report({"INFO"}, "Переименовывать нечего")
            return {"CANCELLED"}
        mode = ".L/.R" if to_mirror else "Sims 3"
        _log(
            "переименовано костей: %d, вершинных групп: %d, f-curve: %d → режим %s"
            % (info["bones"], info["vertex_groups"], info["fcurves"], mode)
        )
        _write_log_block(_drain_log())
        self.report(
            {"INFO"},
            "%s: костей %d, групп весов %d" % (mode, info["bones"], info["vertex_groups"]),
        )
        return {"FINISHED"}


class S3CLIP_OT_clear_anim(bpy.types.Operator):
    """Снять анимацию с рига"""

    bl_idname = "s3clipfbx.clear_anim"
    bl_label = "Очистить анимацию рига"

    def execute(self, context):
        settings = _settings(context)
        arm = _resolve_armature(context, settings)
        if arm is None:
            return {"CANCELLED"}
        bl.clear_animation(arm)
        bl.reset_pose(arm)
        return {"FINISHED"}


# --------------------------------------------------------------------------
# Панель
# --------------------------------------------------------------------------
class S3CLIP_PT_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "S3 Clip"
    bl_label = "Sims 3 Clip → FBX"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        arm = _resolve_armature(context, settings)

        box = layout.box()
        box.label(text="Риг", icon="ARMATURE_DATA")
        box.prop(settings, "armature", text="")
        if arm is None:
            box.label(text="Выберите арматуру Sims 3", icon="ERROR")
        else:
            box.label(text="%s · %d костей" % (arm.name, len(arm.data.bones)), icon="CHECKMARK")

        box = layout.box()
        box.label(text="Источник", icon="FILE_FOLDER")
        box.prop(settings, "input_dir", text="")
        box.prop(settings, "output_dir", text="")
        box.prop(settings, "recursive")
        box.prop(settings, "mode", text="")
        box.prop(settings, "format", text="")
        if settings.mode == "separate":
            row = box.row(align=True)
            row.prop(settings, "name_template")
            box.prop(settings, "skip_existing")
        else:
            box.prop(settings, "single_file_name")
            if settings.format == "gltf":
                box.prop(settings, "chunk_size")

        col = layout.column(align=True)
        col.scale_y = 1.3
        suffix = "glTF" if settings.format == "gltf" else "FBX"
        col.operator("s3clipfbx.batch_dir", text="Пакет: папка → " + suffix, icon="EXPORT")
        col.operator("s3clipfbx.batch_files", text="Пакет: выбрать файлы…", icon="FILEBROWSER")

        layout.separator()
        col = layout.column(align=True)
        col.operator("s3clipfbx.import_clip", text="Импорт одного клипа", icon="IMPORT")
        col.operator("s3clipfbx.check_clips", text="Проверить клип(ы)", icon="VIEWZOOM")
        col.operator("s3clipfbx.analyze_clip", text="Диагностика клипа…", icon="QUESTION")
        col.operator("s3clipfbx.dump_rig_map", text="Хеши костей рига", icon="SORTALPHA")
        col.operator("s3clipfbx.clear_anim", text="Очистить анимацию", icon="TRASH")

        box = layout.box()
        box.label(text="Симметрия весов", icon="MOD_MIRROR")
        if arm is not None:
            mode = ".L/.R" if bl.has_mirror_names(arm) else "Sims 3 (b__L_…)"
            box.label(text="Текущий режим: %s" % mode)
        box.operator(
            "s3clipfbx.mirror_names",
            text="Переключить имена костей",
            icon="ARMATURE_DATA",
        )
        box.label(text="Исходные имена сохраняются — клипы", icon="BLANK1")
        box.label(text="сопоставляются в любом режиме", icon="BLANK1")

        layout.separator()
        box = layout.box()
        box.label(text="Бейк", icon="KEYFRAME")
        box.prop(settings, "dense")
        box.prop(settings, "interpolation", text="")
        box.prop(settings, "positions", text="")
        sub = box.column(align=True)
        sub.enabled = settings.positions == "root"
        sub.prop(settings, "ai_positions_root", text="Корень")
        if arm is not None and settings.positions == "root":
            chain = bl.skeleton_from_armature(arm).root_chain_names()
            box.label(text="Цепочка: " + (" → ".join(chain) if chain else "не найдена"), icon="BLANK1")

        if settings.positions == "motion":
            sub = box.column(align=True)
            sub.prop(settings, "motion_horizontal", text="Покачивание")
            sub.prop(settings, "motion_scale", text="Амплитуда")
            sub.prop(settings, "ground_snap")
            adv = box.column(align=True)
            adv.prop(settings, "motion_ref", text="")
            adv.prop(settings, "motion_ref_frame")
            adv.prop(settings, "motion_unit_source")
            adv.prop(settings, "motion_unit_target")
            adv.prop(settings, "motion_service")
        box.prop(settings, "missing_position", text="")
        box.prop(settings, "auto_convention")
        sub = box.column(align=True)
        sub.enabled = not settings.auto_convention
        sub.prop(settings, "position_mode", text="")
        sub.prop(settings, "rotation_mode", text="")
        row = box.row(align=True)
        row.prop(settings, "frame_padding")
        row.prop(settings, "scale")

        box = layout.box()
        box.label(text="FBX", icon="EXPORT")
        box.prop(settings, "include_meshes")
        box.prop(settings, "all_bones")
        box.prop(settings, "deform_bones_only")
        box.prop(settings, "add_leaf_bones")
        row = box.row(align=True)
        row.prop(settings, "axis_forward", text="")
        row.prop(settings, "axis_up", text="")
        box.prop(settings, "keep_actions")


classes = (
    S3ClipFbxSettings,
    S3CLIP_OT_import_clip,
    S3CLIP_OT_batch_dir,
    S3CLIP_OT_batch_files,
    S3CLIP_OT_check_clips,
    S3CLIP_OT_analyze_clip,
    S3CLIP_OT_dump_rig_map,
    S3CLIP_OT_mirror_names,
    S3CLIP_OT_clear_anim,
    S3CLIP_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.s3clip_fbx = PointerProperty(type=S3ClipFbxSettings)


def unregister():
    del bpy.types.Scene.s3clip_fbx
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
