"""Диагностика «почему .glb пустой» — запускается внутри Blender.

    blender -b "риг.blend" -P tools/diag_export.py -- --clips "папка с клипами"

Печатает по порядку:
  1. версию Blender;
  2. все объекты и их видимость (частая причина пустого экспорта — арматура в
     исключённой коллекции или скрыта);
  3. арматуры: сколько костей, есть ли ключевые кости Sims 3;
  4. 3 клипа: сколько треков сопоставилось с ригом («без пары» должно быть 0);
  5. пробный экспорт одного клипа: размер файла и число анимаций внутри .glb;
  6. список опций glTF-экспортёра, связанных с анимацией и выбором объектов
     (чтобы увидеть, не переименовали ли их в этой версии Blender).
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from io_s3_clip_fbx import blender as bl  # noqa: E402
from io_s3_clip_fbx.s3clip import bake as s3bake  # noqa: E402
from io_s3_clip_fbx.s3clip.clipfile import read_clip  # noqa: E402

KEY_BONES = ("b__ROOT__", "b__ROOT_bind__", "b__Pelvis__", "b__Spine0__", "b__Head__")


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    clips = ""
    for i, arg in enumerate(argv):
        if arg in ("--clips", "-c") and i + 1 < len(argv):
            clips = argv[i + 1]
    return clips


def glb_info(path: str) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(12)
            if len(head) < 12 or head[:4] != b"glTF":
                return "размер %d Б, но это не glTF" % size
            length, _ctype = struct.unpack_from("<II", fh.read(8), 0)
            data = json.loads(fh.read(length).decode("utf-8"))
        anims = data.get("animations", [])
        nodes = data.get("nodes", [])
        skins = data.get("skins", [])
        return "размер %d Б | анимаций %d | узлов %d | скинов %d" % (size, len(anims), len(nodes), len(skins))
    except Exception as exc:  # noqa: BLE001
        return "не прочитать: %s" % exc


def main() -> int:
    clips_dir = parse_args()
    print("\n================ ДИАГНОСТИКА ЭКСПОРТА ================")
    print("Blender: %s" % bpy.app.version_string)
    print("файл   : %s" % bpy.data.filepath)

    print("\n-- объекты в сцене --")
    for obj in bpy.context.scene.objects:
        collections = [c.name for c in obj.users_collection]
        print("  %-28s %-10s видим=%s скрыт=%s коллекции=%s"
              % (obj.name[:28], obj.type, not obj.hide_viewport, not obj.hide_get(), collections))

    print("\n-- коллекции (исключённые не экспортируются и не выделяются) --")
    for layer_coll in bpy.context.view_layer.layer_collection.children:
        print("  %-28s исключена=%s" % (layer_coll.name, layer_coll.exclude))

    armatures = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    print("\n-- арматуры: %d --" % len(armatures))
    chosen = None
    for arm in armatures:
        bones = arm.data.bones
        names = [b.name for b in bones]
        has = {name: (name in names) for name in KEY_BONES}
        print("  %s: костей %d" % (arm.name, len(bones)))
        print("    первые кости: %s" % ", ".join(names[:6]))
        print("    ключевые: %s" % ", ".join("%s=%s" % (k, "да" if v else "НЕТ") for k, v in has.items()))
        try:
            skel = bl.skeleton_from_armature(arm)
            mapping = skel.hash_map()
            print("    хеш-таблица костей: %d" % len(mapping))
        except Exception as exc:  # noqa: BLE001
            print("    !! skeleton_from_armature упал: %s" % exc)
        if chosen is None:
            chosen = arm

    if chosen is None:
        print("\n!! в файле нет ни одной арматуры — экспортировать нечего")
        return 1

    print("\n-- выбор арматуры, как это делает аддон --")
    found = bl.find_armature(bpy.context, None)
    print("  bl.find_armature → %s" % (found.name if found else "None (не нашёл!)"))
    try:
        bpy.ops.object.select_all(action="DESELECT")
        chosen.select_set(True)
        bpy.context.view_layer.objects.active = chosen
        print("  select_set(True) прошёл, выделено объектов: %d"
              % len([o for o in bpy.context.view_layer.objects if o.select_get()]))
    except Exception as exc:  # noqa: BLE001
        print("  !! не удалось выделить арматуру: %s" % exc)

    if clips_dir and os.path.isdir(clips_dir):
        clips = sorted(f for f in os.listdir(clips_dir) if f.endswith(".animation"))[:3]
        print("\n-- сопоставление клипов с ригом (%d шт.) --" % len(clips))
        skel = bl.skeleton_from_armature(chosen)
        for name in clips:
            try:
                anim = read_clip(os.path.join(clips_dir, name))
                result = s3bake.bake_clip(anim, skel, s3bake.BakeOptions(dense=False))
                print("  %-46s треков %3d | сопоставлено %3d | без пары %d"
                      % (name[:46], len(anim.tracks), result.matched_bones,
                         len(result.unmatched_hashes)))
                for key in result.unmatched_hashes[:3]:
                    print("      нет кости для ключа %08X" % key)
            except Exception as exc:  # noqa: BLE001
                print("  %-46s ОШИБКА: %s" % (name[:46], exc))

        if clips:
            print("\n-- пробный экспорт одного клипа --")
            tmp = tempfile.mkdtemp(prefix="s3diag_")
            out = os.path.join(tmp, "one.glb")
            try:
                anim = read_clip(os.path.join(clips_dir, clips[0]))
                action, result = bl.build_action(chosen, anim, s3bake.BakeOptions())
                ad = chosen.animation_data or chosen.animation_data_create()
                ad.action = action
                if hasattr(ad, "action_slot") and getattr(action, "slots", None) and ad.action_slot is None:
                    ad.action_slot = action.slots[0]
                print("  экшен: %s, кривых %d" % (action.name, len(action.fcurves)))
                bl.export_gltf(bpy.context, out, chosen, include_meshes=False, nla_strips=False)
                print("  результат: %s" % glb_info(out))
            except Exception as exc:  # noqa: BLE001
                print("  !! экспорт упал: %s" % exc)
            print("  файл: %s" % out)

    print("\n-- опции glTF-экспортёра про анимацию и выбор --")
    try:
        props = bpy.ops.export_scene.gltf.get_rna_type().properties.keys()
        interesting = [p for p in props if any(k in p for k in ("anim", "selection", "skin", "apply", "yup", "format"))]
        print("  %s" % ", ".join(sorted(interesting)))
    except Exception as exc:  # noqa: BLE001
        print("  !! не получил список опций: %s" % exc)

    print("\n================ КОНЕЦ ДИАГНОСТИКИ ================\n")
    return 0


main()
