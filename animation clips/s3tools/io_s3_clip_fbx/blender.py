"""Blender-слой: скелет → экшены → FBX."""

from __future__ import annotations

import json
import os
import re

import bpy

from .s3clip import bake as s3bake
from .s3clip import naming
from .s3clip.clipfile import ClipAnim
from .s3clip.mat import Mat4

S3_ADDON_ID = "io_s3_clip_fbx"

_INTERP_VALUES = {
    item.identifier: item.value
    for item in bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items
}


# --------------------------------------------------------------------------
# Скелет
# --------------------------------------------------------------------------
def skeleton_from_armature(arm_obj: bpy.types.Object) -> s3bake.Skeleton:
    bones = arm_obj.data.bones
    index = {b.name: i for i, b in enumerate(bones)}
    joints = []
    for bone in bones:
        flat: Mat4 = tuple(v for row in bone.matrix_local for v in row)
        aliases = []
        stored = bone.get(naming.S3_NAME_PROP)
        if isinstance(stored, str) and stored and stored != bone.name:
            aliases.append(stored)
        joints.append(
            s3bake.JointRest(
                name=bone.name,
                parent=index[bone.parent.name] if bone.parent else -1,
                matrix_local=flat,
                aliases=tuple(aliases),
            )
        )
    return s3bake.Skeleton(joints)


def armature_bone_names(arm_obj: bpy.types.Object):
    return [b.name for b in arm_obj.data.bones]


def find_armature(context, explicit=None) -> bpy.types.Object | None:
    if explicit is not None and explicit.type == "ARMATURE":
        return explicit
    obj = context.active_object
    if obj is not None:
        if obj.type == "ARMATURE":
            return obj
        for mod in obj.modifiers:
            if mod.type == "ARMATURE" and mod.object:
                return mod.object
    candidates = [o for o in context.scene.objects if o.type == "ARMATURE"]
    if len(candidates) == 1:
        return candidates[0]
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------
# Экшены
# --------------------------------------------------------------------------
def _new_fcurve(action, data_path: str, index: int, group_name: str):
    try:  # Blender <= 4.3 и legacy-слой в 4.4+
        return action.fcurves.new(data_path, index=index, action_group=group_name)
    except Exception:
        pass
    # современный API со слотами (Blender 4.4+)
    if not action.slots:
        action.slots.new(id_type="OBJECT", name="Armature")
    slot = action.slots[0]
    if not action.layers:
        action.layers.new("Layer")
    layer = action.layers[0]
    if not layer.strips:
        layer.strips.new(type="KEYFRAME")
    strip = layer.strips[0]
    bag = strip.channelbag(slot, ensure=True)
    return bag.fcurves.new(data_path, index=index)


def _fill_fcurve(fcurve, frames, values, interpolation: str, group=None):
    count = len(frames)
    if not count:
        return
    fcurve.keyframe_points.add(count)
    flat = []
    for frame, value in zip(frames, values):
        flat.append(float(frame))
        flat.append(float(value))
    fcurve.keyframe_points.foreach_set("co", flat)
    code = _INTERP_VALUES.get(interpolation.upper(), 1)
    fcurve.keyframe_points.foreach_set("interpolation", [code] * count)
    try:
        fcurve.update()
    except Exception:
        pass


def _assign_action(arm_obj, action):
    ad = arm_obj.animation_data or arm_obj.animation_data_create()
    ad.action = action
    if hasattr(ad, "action_slot"):
        try:
            if action.slots and ad.action_slot is None:
                ad.action_slot = action.slots[0]
        except Exception:
            pass
    return ad


def build_action(
    arm_obj: bpy.types.Object,
    anim: ClipAnim,
    options: s3bake.BakeOptions | None = None,
    action_name: str | None = None,
    interpolation: str = "LINEAR",
    variants=None,
):
    """Создаёт экшен на арматуре и возвращает (action, BakeResult)."""
    options = options or s3bake.BakeOptions()
    skeleton = skeleton_from_armature(arm_obj)

    detected = None
    if options.auto_convention:
        from .s3clip import diagnose as s3diag

        order = anim.field_order
        pos_mode, rot_mode = options.position_mode, options.rotation_mode
        scores = {}
        best_score = 0.0

        if variants and len(variants) > 1:
            order, pos_mode, rot_mode, order_scores = s3diag.detect_field_order(
                variants, skeleton, options
            )
            anim = variants[order]
            scores = order_scores
            best_score = order_scores.get(order, 0.0)
            print(
                "[S3 Clip→FBX] порядок полей в кривых: %s (сдвиг от rest: %s)"
                % (order, {k: round(v, 2) for k, v in order_scores.items()})
            )
        else:
            pos_mode, rot_mode, conv_scores = s3diag.detect_convention(anim, skeleton, options)
            scores = conv_scores
            best_score = conv_scores.get((pos_mode, rot_mode), 0.0)
            if (pos_mode, rot_mode) != ("absolute", "absolute"):
                print(
                    "[S3 Clip→FBX] авто-конвенция: позиции=%s, повороты=%s (сдвиг от rest %.2f м)"
                    % (pos_mode, rot_mode, best_score)
                )

        detected = (pos_mode, rot_mode, scores)
        # танец/бег честно уводит кисти на 0.5–1 м от rest, так что порог выше
        if best_score > 2.5:
            print(
                "[S3 Clip→FBX] ВНИМАНИЕ: даже лучший вариант уводит кости на %.2f м от rest. "
                "Похоже на проблему в самом файле — запустите «Диагностика клипа»." % best_score
            )

        options = s3bake.BakeOptions(
            dense=options.dense,
            interpolate=options.interpolate,
            frame_start=options.frame_start,
            frame_end=options.frame_end,
            missing_position=options.missing_position,
            root_rotation=options.root_rotation,
            position_mode=pos_mode,
            rotation_mode=rot_mode,
            scale=options.scale,
            positions=options.positions,
            positions_root_bone=options.positions_root_bone,
            motion_ref=options.motion_ref,
            motion_ref_frame=options.motion_ref_frame,
            motion_unit_source=options.motion_unit_source,
            motion_unit_target=options.motion_unit_target,
            motion_scale=options.motion_scale,
            motion_horizontal=options.motion_horizontal,
            motion_service=options.motion_service,
            ground_snap=options.ground_snap,
            auto_convention=False,
        )

    _log_positions_mode(skeleton, options)
    _log_motion_mode(anim, skeleton, options)
    result = s3bake.bake_clip(anim, skeleton, options)

    name = action_name or (anim.name or os.path.basename(anim.path) or "s3_clip")
    action = bpy.data.actions.new(name)
    action.use_fake_user = False

    for bone_curves in result.bones:
        bone_name = bone_curves.name
        data_path = 'pose.bones["%s"].' % bone_name
        if bone_curves.positions:
            frames = [f for f, _v in bone_curves.positions]
            for axis in range(3):
                values = [v[axis] for _f, v in bone_curves.positions]
                fc = _new_fcurve(action, data_path + "location", axis, bone_name)
                _fill_fcurve(fc, frames, values, interpolation)
        if bone_curves.rotations:
            frames = [f for f, _v in bone_curves.rotations]
            # CLIP хранит xyzw, Blender — wxyz
            for axis, src in enumerate((3, 0, 1, 2)):
                values = [v[src] for _f, v in bone_curves.rotations]
                fc = _new_fcurve(action, data_path + "rotation_quaternion", axis, bone_name)
                _fill_fcurve(fc, frames, values, interpolation)

    # метаданные клипа
    action["s3_clip_name"] = anim.name
    action["s3_source_file"] = anim.source_file
    action["s3_actor_name"] = anim.actor_name
    action["s3_frame_start"] = result.frame_start
    action["s3_frame_end"] = result.frame_end
    action["s3_position_mode"] = options.position_mode
    action["s3_rotation_mode"] = options.rotation_mode
    action["s3_field_order"] = anim.field_order
    if detected:
        action["s3_convention_scores"] = "%s" % {
            (k if isinstance(k, str) else "%s/%s" % k): round(v, 3)
            for k, v in detected[2].items()
        }

    _assign_action(arm_obj, action)
    if options.ground_snap:
        shift = ground_snap(arm_obj, action, result.frame_start, result.frame_end)
        if abs(shift) > 1e-3:
            action["s3_ground_shift"] = round(shift, 4)
            print("[S3 Clip→FBX] прижато к полу: сдвиг корня %+.3f м" % shift)
    # кости, для которых в этом клипе нет кривых, не должны держать позу прошлого,
    # а поза нового клипа должна сразу быть видна в вьюпорте
    reset_pose(arm_obj)
    refresh_pose()
    return action, result


def set_quaternion_mode(arm_obj: bpy.types.Object):
    changed = 0
    for pbone in arm_obj.pose.bones:
        if pbone.rotation_mode != "QUATERNION":
            pbone.rotation_mode = "QUATERNION"
            changed += 1
    return changed


def clear_animation(arm_obj: bpy.types.Object):
    arm_obj.animation_data_clear()
    if arm_obj.data.animation_data:
        arm_obj.data.animation_data_clear()


_POSITION_LOG = set()


def _log_motion_mode(anim, skeleton, options):
    """Печатает, в каких единицах переносится движение (один раз на прогон)."""
    if options.positions != s3bake.POSITIONS_MOTION:
        return
    info = s3bake.motion_summary(anim, skeleton, options)
    key = (round(info["unit_source"], 4), round(info["unit_target"], 4), round(info["factor"], 4))
    if key in _POSITION_LOG:
        return
    _POSITION_LOG.add(key)
    print(
        "[S3 Clip→FBX] движение: единица клипа %.3f м, единица рига %.3f м, множитель %.3f; "
        "двигаются: %s"
        % (
            info["unit_source"],
            info["unit_target"],
            info["factor"],
            ", ".join(info["moving"][:6]) or "ничего (клип на месте)",
        )
    )


_SERVICE_MARKERS = ("info", "pole", "slot", "offset", "world", "export")


def foot_bone_names(arm_obj):
    """Кости стоп для проверки «на полу» (служебные IK-цели отбрасываем).

    `L_footInfo1`, `R_footInfo1` и прочие цели стоят в нуле всегда — если их
    учитывать, пол всегда «на месте» и проверка ничего не значит.
    """
    names = []
    for bone in arm_obj.data.bones:
        low = bone.name.lower()
        if any(marker in low for marker in _SERVICE_MARKERS):
            continue
        if any(marker in low for marker in ("foot", "toe", "ankle")):
            names.append(bone.name)
    return names


def ground_snap(arm_obj, action, frame_start, frame_end, carrier_name=None):
    """Сдвигает кость-носитель так, чтобы самая низкая точка стоп встала на z = 0.

    Возвращает величину сдвига в метрах (по мировому Z).
    """
    from mathutils import Vector

    foot_names = foot_bone_names(arm_obj) or [b.name for b in arm_obj.data.bones]
    # «пол» в rest-позе: у симсовских ригов подошва на 0, а кость стопы висит выше,
    # поэтому целимся не в z=0, а в собственную rest-высоту стоп этого рига
    rest_min = None
    for name in foot_names:
        bone = arm_obj.data.bones.get(name)
        if bone is None:
            continue
        for point in (bone.head_local, bone.tail_local):
            z = (arm_obj.matrix_world @ point).z
            rest_min = z if rest_min is None else min(rest_min, z)
    scene = bpy.context.scene
    lowest = None
    for frame in range(int(frame_start), int(frame_end) + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        for name in foot_names:
            pose_bone = arm_obj.pose.bones.get(name)
            if pose_bone is None:
                continue
            for point in (pose_bone.head, pose_bone.tail):
                z = (arm_obj.matrix_world @ point).z
                lowest = z if lowest is None else min(lowest, z)
    if lowest is None:
        return 0.0
    offset = (rest_min - lowest) if rest_min is not None else -lowest
    if abs(offset) < 1e-4:
        return 0.0

    skeleton = skeleton_from_armature(arm_obj)
    chain = skeleton.root_chain_names()
    carrier = carrier_name or (chain[-1] if chain else None)
    if carrier is None:
        return 0.0
    bone = arm_obj.data.bones.get(carrier)
    if bone is None:
        return 0.0

    # мировое «вверх» в локальные оси кости-носителя (через матрицу арматуры:
    # у симсовских ригов она повёрнута, и «вверх» в её пространстве — это +Y)
    world_up = arm_obj.matrix_world.to_3x3().inverted() @ Vector((0.0, 0.0, 1.0))
    local_dir = bone.matrix_local.to_3x3().inverted() @ world_up
    delta = [offset * local_dir[i] for i in range(3)]

    data_path = 'pose.bones["%s"].location' % carrier
    for axis in range(3):
        if abs(delta[axis]) < 1e-9:
            continue
        fcurve = None
        for candidate in action.fcurves:
            if candidate.data_path == data_path and candidate.array_index == axis:
                fcurve = candidate
                break
        if fcurve is None:
            fcurve = action.fcurves.new(data_path, index=axis, action_group=carrier)
            fcurve.keyframe_points.insert(frame_start, delta[axis])
            fcurve.keyframe_points.insert(frame_end, delta[axis])
        else:
            for point in fcurve.keyframe_points:
                point.co.y += delta[axis]
                point.handle_left.y += delta[axis]
                point.handle_right.y += delta[axis]
        fcurve.update()
    return offset


def _log_positions_mode(skeleton, options):
    """Один раз на прогон пишет, какие кости получают трансляцию."""
    if options.positions != s3bake.POSITIONS_ROOT:
        return
    manual = (options.positions_root_bone or "").strip()
    key = (manual, tuple(skeleton.root_chain_names()))
    if key in _POSITION_LOG:
        return
    _POSITION_LOG.add(key)
    if manual:
        print("[S3 Clip→FBX] позиции: только %s и её предки, остальные держат rest" % manual)
    else:
        print(
            "[S3 Clip→FBX] позиции: корневая цепочка %s, остальные кости держат rest"
            % " → ".join(skeleton.root_chain_names())
        )


def refresh_pose():
    """Заставляет Blender пересчитать позу по текущему экшену на текущем кадре.

    Просто `frame_set(тот же кадр)` ничего не пересчитывает, поэтому прыгаем
    на соседний кадр и обратно.
    """
    scene = bpy.context.scene
    frame = scene.frame_current
    try:
        scene.frame_set(frame + 1)
        scene.frame_set(frame)
    except Exception:  # noqa: BLE001
        bpy.context.view_layer.update()


def reset_pose(arm_obj: bpy.types.Object):
    for pbone in arm_obj.pose.bones:
        pbone.location = (0.0, 0.0, 0.0)
        pbone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pbone.rotation_euler = (0.0, 0.0, 0.0)
        pbone.scale = (1.0, 1.0, 1.0)


# --------------------------------------------------------------------------
# FBX
# --------------------------------------------------------------------------
def _supported_kwargs(op, kwargs: dict) -> dict:
    try:
        props = set(op.get_rna_type().properties.keys())
    except Exception:
        return kwargs
    return {k: v for k, v in kwargs.items() if k in props}


def ensure_fbx_addon() -> bool:
    if hasattr(bpy.ops.export_scene, "fbx"):
        return True
    try:
        bpy.ops.preferences.addon_enable(module="io_scene_fbx")
    except Exception:
        return False
    return hasattr(bpy.ops.export_scene, "fbx")


def _select_only(context, objects):
    for obj in context.view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    if objects:
        context.view_layer.objects.active = objects[0]


def export_fbx(
    context,
    filepath: str,
    arm_obj: bpy.types.Object,
    *,
    include_meshes: bool = False,
    scale: float = 1.0,
    add_leaf_bones: bool = False,
    axis_forward: str = "-Z",
    axis_up: str = "Y",
    nla_strips: bool = False,
    bake_all_actions: bool = False,
    all_bones: bool = True,
    deform_bones_only: bool = False,
):
    """Экспортирует арматуру (и, при желании, её меши) в FBX."""
    ensure_fbx_addon()
    previous_selection = [obj for obj in context.view_layer.objects if obj.select_get()]
    previous_active = context.view_layer.objects.active
    objects = [arm_obj]
    if include_meshes:
        for child in arm_obj.children_recursive:
            if child.type == "MESH":
                objects.append(child)
    _select_only(context, objects)

    if not nla_strips and not bake_all_actions:
        ad = arm_obj.animation_data
        if ad is not None and ad.action is not None and hasattr(ad, "action_slot"):
            try:
                if ad.action.slots and ad.action_slot is None:
                    ad.action_slot = ad.action.slots[0]
            except Exception:
                pass

    object_types = {"ARMATURE"}
    if include_meshes:
        object_types.add("MESH")

    kwargs = dict(
        filepath=filepath,
        use_selection=True,
        use_active_collection=False,
        object_types=object_types,
        apply_scale_options="FBX_SCALE_NONE",
        global_scale=scale,
        apply_unit_scale=True,
        axis_forward=axis_forward,
        axis_up=axis_up,
        use_space_transform=True,
        bake_space_transform=False,
        add_leaf_bones=add_leaf_bones,
        primary_bone_axis="Y",
        secondary_bone_axis="X",
        use_armature_deform_only=deform_bones_only,
        armature_nodetype="NULL",
        bake_anim=True,
        bake_anim_use_all_bones=all_bones,
        bake_anim_use_nla_strips=nla_strips,
        bake_anim_use_all_actions=bake_all_actions,
        bake_anim_force_startend_keying=True,
        bake_anim_step=1.0,
        bake_anim_simplify_factor=0.0,
        use_mesh_modifiers=False,
        mesh_smooth_type="FACE",
        path_mode="AUTO",
        embed_textures=False,
        use_custom_props=False,
        bake_space_transform_=False,
    )
    kwargs.pop("bake_space_transform", None)
    kwargs = _supported_kwargs(bpy.ops.export_scene.fbx, kwargs)

    os.makedirs(os.path.dirname(os.path.abspath(filepath)) or ".", exist_ok=True)
    try:
        result = bpy.ops.export_scene.fbx(**kwargs)
    finally:
        # возвращаем выделение пользователя
        try:
            for obj in context.view_layer.objects:
                obj.select_set(False)
            for obj in previous_selection:
                try:
                    obj.select_set(True)
                except Exception:
                    pass
            context.view_layer.objects.active = previous_active
        except Exception:
            pass
    if "FINISHED" not in result:
        raise RuntimeError("экспорт FBX не удался: %s" % (result,))
    return filepath


def glb_summary(path: str) -> dict:
    """Что внутри .glb: анимации, узлы, меши (имена), скины. Пустой словарь — не читается."""
    try:
        import struct as _struct

        with open(path, "rb") as fh:
            head = fh.read(12)
            if len(head) < 12 or head[:4] != b"glTF":
                return {}
            length, _ctype = _struct.unpack_from("<II", fh.read(8), 0)
            data = json.loads(fh.read(length).decode("utf-8"))
        meshes = data.get("meshes", [])
        return {
            "animations": len(data.get("animations", [])),
            "nodes": len(data.get("nodes", [])),
            "meshes": len(meshes),
            "mesh_names": [m.get("name", "?") for m in meshes[:8]],
            "skins": len(data.get("skins", [])),
        }
    except Exception:  # noqa: BLE001
        return {}


def glb_animation_count(path: str) -> int:
    """Сколько анимаций внутри .glb (-1 — файл не читается)."""
    info = glb_summary(path)
    return info.get("animations", -1) if info else -1


def export_gltf(
    context,
    filepath: str,
    arm_obj: bpy.types.Object,
    *,
    include_meshes: bool = False,
    nla_strips: bool = False,
    animation_mode: str | None = None,
    optimize: bool = True,
):
    """Экспортирует арматуру (и меши) в glTF 2.0 (.glb).

    Для Godot это основной формат: glTF рекомендован самим движком, а один .glb
    может содержать несколько анимаций (скелет + N клипов в одном файле).

    animation_mode: 'ACTIONS' (каждый экшен = отдельная анимация) или
    'NLA_TRACKS' (каждый NLA-трек = отдельная анимация). По умолчанию
    выбирается по флагу nla_strips.
    """
    previous_selection = [obj for obj in context.view_layer.objects if obj.select_get()]
    previous_active = context.view_layer.objects.active
    objects = [arm_obj]
    if include_meshes:
        for child in arm_obj.children_recursive:
            if child.type == "MESH":
                objects.append(child)
    _select_only(context, objects)

    ad = arm_obj.animation_data
    if ad is not None and ad.action is not None and hasattr(ad, "action_slot"):
        try:
            if ad.action.slots and ad.action_slot is None:
                ad.action_slot = ad.action.slots[0]
        except Exception:
            pass

    if animation_mode is None:
        # ACTIVE_ACTIONS — только экшен, назначенный сейчас. ACTIONS тащит в файл ВСЕ
        # экшены сцены (с fake user от предыдущих клипов), из-за чего в .glb оказывалось
        # по 5 анимаций и файл раздувался в 5 раз.
        animation_mode = "NLA_TRACKS" if nla_strips else "ACTIVE_ACTIONS"

    # дополнительная страховка: снимаем fake user с чужих экшенов, чтобы режим ACTIONS
    # (если он всё-таки включится) их не подхватил
    fake_saved = {}
    ad_now = arm_obj.animation_data
    assigned = ad_now.action if ad_now is not None else None
    for act in bpy.data.actions:
        if act is assigned or not act.use_fake_user:
            continue
        fake_saved[act.name] = True
        act.use_fake_user = False

    # Оптимизация «выбросить неизменяющиеся каналы» опирается на
    # export_optimize_animation_keep_anim_armature. В некоторых сборках Blender
    # этого параметра нет — тогда оптимизация выкидывает ВСЁ и файл выходит пустым.
    props = set()
    try:
        props = set(bpy.ops.export_scene.gltf.get_rna_type().properties.keys())
    except Exception:
        pass
    optimize_supported = "export_optimize_animation_keep_anim_armature" in props if props else False
    if optimize and not optimize_supported:
        if not getattr(export_gltf, "_warned", False):
            print("[S3 Clip→FBX] . в этой версии Blender нет export_optimize_animation_keep_anim_armature:"
                  " печём без оптимизации каналов (файлы крупнее, но рабочие)")
            export_gltf._warned = True  # type: ignore[attr-defined]
        optimize = False

    kwargs = dict(
        filepath=filepath,
        export_format="GLB",
        use_selection=True,
        export_apply=False,
        export_yup=True,
        export_skins=True,
        export_def_bones=False,
        export_leaf_bone=False,
        export_animations=True,
        export_animation_mode=animation_mode,
        export_bake_animation=False,
        export_force_sampling=True,
        export_frame_step=1,
        export_current_frame=False,
        export_anim_single_armature=True,
        export_reset_pose_bones=True,
        # убирает каналы, которые не меняются (у нас в клипе двигается 32-61 кость
        # из 257) — файл и память в движке меньше в разы, поза не страдает
        export_optimize_animation_size=optimize,
        export_optimize_animation_keep_anim_armature=True,
        export_morph=True,
        export_morph_animation=True,
        export_extras=False,
    )
    os.makedirs(os.path.dirname(os.path.abspath(filepath)) or ".", exist_ok=True)

    # Разные версии Blender по-разному относятся к оптимизации, выделению и режиму
    # действий. Если файл вышел пустым (0 анимаций), перебираем варианты, пока
    # не получим живой. Так 4.4/4.3 тоже отработают, хоть и медленнее.
    # Пока печём, прячем всё лишнее (формы костей-«сферы», меши тела, свет):
    # так даже режим «вся сцена» не затянет геометрию в файл.
    hidden_state = {}
    if not include_meshes:
        for obj in context.view_layer.objects:
            if obj in objects:
                continue
            try:
                hidden_state[obj.name] = obj.hide_viewport
                obj.hide_viewport = True
            except Exception:  # noqa: BLE001
                pass

    variants = [
        ("как есть", {}),
        ("без оптимизации каналов", {"export_optimize_animation_size": False}),
        ("без выделения (вся сцена)", {"use_selection": False, "use_visible": True}),
        ("без оптимизации и без выделения", {"export_optimize_animation_size": False,
                                             "use_selection": False}),
        ("сброс поз + вся сцена", {"use_selection": False, "use_visible": True,
                                   "export_reset_pose_bones": False,
                                   "export_optimize_animation_size": False,
                                   "export_bake_animation": True}),
    ]
    last_error = None
    for label, overrides in variants:
        attempt = dict(kwargs)
        attempt.update(overrides)
        attempt = _supported_kwargs(bpy.ops.export_scene.gltf, attempt)
        attempt["filepath"] = filepath
        try:
            result = bpy.ops.export_scene.gltf(**attempt)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        info = glb_summary(filepath)
        count = info.get("animations", 0) if info else 0
        meshes = info.get("meshes", 0) if info else 0
        single_ok = nla_strips or count == 1
        if "FINISHED" in result and count > 0 and (include_meshes or meshes == 0) and single_ok:
            if label != "как есть":
                print("[S3 Clip→FBX] . экспорт ожил только в режиме «%s»" % label)
            last_error = None
            break
        if count > 0 and meshes > 0 and not include_meshes:
            # в файл уехала геометрия (костяные «сферы», меши тела) — это лишние мегабайты
            print("[S3 Clip→FBX] . в файл попали меши (%d: %s) — повторяю только по арматуре"
                  % (meshes, ", ".join(info.get("mesh_names", [])[:3])))
            last_error = RuntimeError("%s: в файле %d мешей" % (label, meshes))
            continue
        last_error = RuntimeError("%s: анимаций в файле %s (ожидали 1)" % (label, count))

    for name in fake_saved:
        act = bpy.data.actions.get(name)
        if act is not None:
            act.use_fake_user = True

    # вернуть видимость, выделение и активный объект как было
    for name, was_hidden in hidden_state.items():
        obj = bpy.data.objects.get(name)
        if obj is not None:
            try:
                obj.hide_viewport = was_hidden
            except Exception:  # noqa: BLE001
                pass

    try:
        for obj in context.view_layer.objects:
            obj.select_set(False)
        for obj in previous_selection:
            try:
                obj.select_set(True)
            except Exception:
                pass
        context.view_layer.objects.active = previous_active
    except Exception:
        pass

    if last_error is not None:
        raise RuntimeError("экспорт glTF не удался ни в одном режиме: %s" % (last_error,))
    return filepath


# --------------------------------------------------------------------------
# Имена файлов
# --------------------------------------------------------------------------
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(name: str, fallback: str = "clip") -> str:
    name = (name or "").strip()
    name = _BAD_CHARS.sub("_", name)
    name = name.strip(" .")
    return name or fallback


def output_name(template: str, anim: ClipAnim, path: str, index: int, ext: str = ".fbx") -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    clip = anim.name or stem
    try:
        rendered = template.format(name=clip, stem=stem, index=index, actor=anim.actor_name or "")
    except Exception:
        rendered = clip
    return sanitize_filename(rendered) + ext


# --------------------------------------------------------------------------
# Переименование костей Sims 3 ⇄ .L/.R (симметрия весов)
# --------------------------------------------------------------------------
import re as _re

_BONE_PATH = _re.compile(r'^pose\.bones\["([^"]+)"\]\.')


def iter_action_fcurves(action):
    """F-curves экшена для legacy-API и для слотового API Blender 4.4+."""
    try:
        curves = list(action.fcurves)
    except Exception:
        curves = []
    if curves:
        yield from curves
        return
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for slot in getattr(action, "slots", []):
                try:
                    bag = strip.channelbag(slot)
                except Exception:
                    bag = None
                if bag is not None:
                    yield from bag.fcurves


def _rename_vertex_groups(arm_obj, mapping):
    """Переименовывает вершинные группы на мешах, привязанных к ригу."""
    targets = set()
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for mod in obj.modifiers:
            if mod.type == "ARMATURE" and mod.object == arm_obj:
                targets.add(obj)
                break
        else:
            if obj.parent == arm_obj:
                targets.add(obj)
    touched = 0
    for obj in targets:
        for group in obj.vertex_groups:
            new_name = mapping.get(group.name)
            if not new_name:
                continue
            if new_name in obj.vertex_groups and obj.vertex_groups[new_name] != group:
                continue  # не плодим дубликаты
            group.name = new_name
            touched += 1
    return touched


def _rename_fcurve_paths(mapping):
    touched = 0
    for action in bpy.data.actions:
        for fcurve in iter_action_fcurves(action):
            match = _BONE_PATH.match(fcurve.data_path)
            if not match:
                continue
            bone_name = match.group(1)
            new_name = mapping.get(bone_name)
            if not new_name:
                continue
            fcurve.data_path = fcurve.data_path.replace(
                'pose.bones["%s"]' % bone_name, 'pose.bones["%s"]' % new_name, 1
            )
            touched += 1
    return touched


def rename_bones_side_mode(arm_obj, to_mirror: bool):
    """Переименовывает кости в `.L/.R` (или обратно) с сохранением исходных имён.

    Возвращает dict с количеством переименованных костей, вершинных групп и f-curve.
    """
    from .s3clip import naming

    bones = arm_obj.data.bones
    existing = {bone.name for bone in bones}
    mapping_plan = {}

    for bone in bones:
        if to_mirror:
            if naming.is_mirror_name(bone.name):
                continue
            new_name = naming.to_mirror(bone.name)
            if not new_name or new_name in existing:
                continue
            mapping_plan[bone.name] = new_name
        else:
            stored = bone.get(naming.S3_NAME_PROP)
            if not naming.is_mirror_name(bone.name) and not stored:
                continue
            new_name = naming.from_mirror(bone.name, stored if isinstance(stored, str) else None)
            if not new_name or new_name in existing:
                continue
            mapping_plan[bone.name] = new_name

    if not mapping_plan:
        return {"bones": 0, "vertex_groups": 0, "fcurves": 0, "mapping": {}}

    # сохраняем исходные имена (для точного возврата)
    for old_name, new_name in mapping_plan.items():
        bone = bones[old_name]
        if to_mirror:
            if naming.S3_NAME_PROP not in bone.keys():
                bone[naming.S3_NAME_PROP] = old_name
        else:
            try:
                del bone[naming.S3_NAME_PROP]
            except Exception:
                pass

    for old_name, new_name in mapping_plan.items():
        bones[old_name].name = new_name

    groups = _rename_vertex_groups(arm_obj, mapping_plan)
    curves = _rename_fcurve_paths(mapping_plan)
    return {
        "bones": len(mapping_plan),
        "vertex_groups": groups,
        "fcurves": curves,
        "mapping": mapping_plan,
    }


def has_mirror_names(arm_obj) -> bool:
    """Есть ли на риге кости в режиме .L/.R."""
    from .s3clip import naming

    for bone in arm_obj.data.bones:
        if naming.is_mirror_name(bone.name) or naming.S3_NAME_PROP in bone.keys():
            return True
    return False
