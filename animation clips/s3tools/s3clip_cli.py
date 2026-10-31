"""Пакетная конвертация Sims 3 .animation → .fbx из командной строки.

Примеры
-------
Один FBX на клип:

    blender -b /путь/amRig.blend --python s3clip_cli.py -- \
        --input demo_clips --output fbx_out

Все клипы в один FBX (каждый клип = отдельный take/NLA-трек):

    blender -b amRig.blend --python s3clip_cli.py -- \
        --input "C:/clips" --output "C:/fbx" --mode single --file-name all_anims.fbx

Только проверить файлы (ничего не экспортировать):

    blender -b amRig.blend --python s3clip_cli.py -- --input "C:/clips" --check

Подсказка: если риг не указан, берётся первая арматура в файле.
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from io_s3_clip_fbx import batch as batch_mod  # noqa: E402
from io_s3_clip_fbx import blender as bl  # noqa: E402
from io_s3_clip_fbx.s3clip import bake as s3bake  # noqa: E402
from io_s3_clip_fbx.s3clip.clipfile import read_clip, read_clip_variants  # noqa: E402
from io_s3_clip_fbx.s3clip import diagnose as s3diag  # noqa: E402


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(prog="s3clip_cli.py", description="Sims 3 .animation → FBX")
    parser.add_argument("--input", "-i", nargs="+", required=True, help="папка(и) или файлы .animation")
    parser.add_argument("--output", "-o", required=True, help="папка для .fbx")
    parser.add_argument("--rig", "-r", default=None, help="имя арматуры (по умолчанию — первая найденная)")
    parser.add_argument("--mode", "-m", choices=("separate", "single"), default="separate")
    parser.add_argument("--format", "-f", choices=("fbx", "gltf"), default="fbx",
                        help="fbx (Unity, Blender) или gltf (.glb — для Godot; один файл может "
                             "содержать все клипы)")
    parser.add_argument("--template", "-t", default="{name}", help="шаблон имени файла: {name} {stem} {index} {actor}")
    parser.add_argument("--file-name", default="s3_animations.fbx", help="имя общего FBX в режиме single")
    parser.add_argument("--meshes", action="store_true", help="включить в FBX меши рига")
    parser.add_argument("--leaf-bones", action="store_true", help="добавлять листовые кости FBX")
    parser.add_argument("--animated-only", action="store_true",
                        help="выгружать только анимированные кости (файл меньше)")
    parser.add_argument("--deform-bones-only", action="store_true",
                        help="только деформирующие кости (без служебных _slot/Target)")
    parser.add_argument("--scale", type=float, default=1.0, help="множитель позиций")
    parser.add_argument("--no-recursive", action="store_true", help="не заходить в подпапки")
    parser.add_argument("--overwrite", action="store_true", help="перезаписывать существующие файлы")
    parser.add_argument("--chunk-size", type=int, default=0,
                        help="режим single + glTF: бить библиотеку на файлы по N клипов "
                             "(0 — всё в один файл). Имена: <file-name без .glb>_01.glb, _02 …")
    parser.add_argument("--sparse", action="store_true", help="ключи только на «родных» кадрах клипа")
    parser.add_argument("--constant", action="store_true", help="ступенчатая интерполяция")
    parser.add_argument("--positions", choices=("motion", "root", "all", "none"), default="motion",
                        help="какие кривые позиции применять: motion — только движение "
                             "относительно rest, в единицах вашего рига (рекомендуется); "
                             "root — абсолютные значения корневой цепочки; all — как в клипе; "
                             "none — только повороты")
    parser.add_argument("--motion-ref", choices=("frame", "median"), default="frame",
                        help="опорное значение кривой: на кадре (обычно 0) или медиана")
    parser.add_argument("--motion-ref-frame", type=int, default=0, help="номер опорного кадра")
    parser.add_argument("--unit-source", type=float, default=0.0,
                        help="единица скелета клипа (высота bind-корня), 0 — авто")
    parser.add_argument("--unit-target", type=float, default=0.0,
                        help="единица вашего рига, 0 — авто (из rest рига)")
    parser.add_argument("--motion-scale", type=float, default=1.0, help="множитель амплитуд")
    parser.add_argument("--motion-horizontal", type=float, default=1.0,
                        help="сколько оставить от горизонтального покачивания корня (0…1.5)")
    parser.add_argument("--motion-service", action="store_true",
                        help="двигать и служебные кости (Info/ExportPole/slotOffset)")
    parser.add_argument("--no-ground-snap", action="store_true",
                        help="не прижимать персонажа к полу после бейка")
    parser.add_argument("--positions-root", default="",
                        help="имя кости, чья трансляция сохраняется в режиме root "
                             "(по умолчанию — корневая цепочка: b__ROOT__ → b__ROOT_bind__)")
    parser.add_argument("--absolute-missing-pos", action="store_true",
                        help="поведение оригинала для костей без данных позиции")
    parser.add_argument("--keep-actions", action="store_true", help="не удалять созданные экшены")
    parser.add_argument("--check", action="store_true", help="только проверить клипы, без экспорта")
    parser.add_argument("--analyze", action="store_true",
                        help="диагностика конвенции (позиции/повороты) и «кто уехал от rest»")
    parser.add_argument("--no-auto", action="store_true",
                        help="не определять конвенцию автоматически (использовать --pos-mode/--rot-mode)")
    parser.add_argument("--pos-mode", choices=("absolute", "delta"), default="absolute",
                        help="как читать позиции: absolute (как в оригинале) или delta от rest")
    parser.add_argument("--rot-mode", choices=("absolute", "delta"), default="absolute",
                        help="как читать повороты: absolute (как в оригинале) или delta от rest")
    parser.add_argument("--field-order", choices=("auto", "offset_scale", "scale_offset"),
                        default="auto",
                        help="порядок двух float в описании кривой: auto — определить по файлу")
    return parser.parse_args(argv)


def find_armature(name):
    if name:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "ARMATURE":
            raise SystemExit("арматура '%s' не найдена в файле" % name)
        return obj
    candidates = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    if not candidates:
        raise SystemExit("в файле нет арматуры: откройте риг Sims 3 (например amRig.blend)")
    return candidates[0]


def read_best(path, skeleton, args):
    """Возвращает (anim, field_order) — с авто-определением порядка полей."""
    if args.field_order != "auto":
        anim = read_clip(path, args.field_order)
        return anim, args.field_order
    variants = read_clip_variants(path)
    if len(variants) == 1:
        anim = next(iter(variants.values()))
        return anim, anim.field_order
    order, _pos, _rot, scores = s3diag.detect_field_order(variants, skeleton)
    print("  порядок полей в кривых: %s (сдвиг от rest: %s)"
          % (order, {k: round(v, 2) for k, v in scores.items()}))
    return variants[order], order


def main():
    args = parse_args()
    arm = find_armature(args.rig)
    files = batch_mod.collect_files(
        args.input, recursive=not args.no_recursive, extensions=(".animation", ".clip", ".s3clip")
    )
    if not files:
        raise SystemExit("не найдено ни одного .animation в %s" % ", ".join(args.input))

    print("[S3 Clip→FBX] риг: %s (%d костей), файлов: %d" % (arm.name, len(arm.data.bones), len(files)))

    if args.analyze:
        bl.set_quaternion_mode(arm)
        skeleton = bl.skeleton_from_armature(arm)
        for path in files:
            try:
                anim, order = read_best(path, skeleton, args)
            except Exception as exc:  # noqa: BLE001
                print("  ОШИБКА %s: %s" % (os.path.basename(path), exc))
                continue
            a = s3diag.analyze_clip(anim, skeleton)
            print("\n=== %s" % os.path.basename(path))
            print("  порядок полей в кривых: %s" % order)
            print("  %.0f fps | кадров: объявлено %d, реально %d | треков %d (сопоставлено %d)"
                  % (a.fps, a.declared_frames, a.actual_frames, a.tracks_total, a.tracks_matched))
            for verdict in a.verdict:
                print("  → %s" % verdict)
            for stat in a.sample[:6]:
                print("    %-26s pos %6.3f / rest %6.3f   rot %5.1f° / rest %5.1f°"
                      % (stat.bone, stat.clip_pos_len, stat.rest_pos_len,
                         stat.clip_rot_deg, stat.rest_rot_deg))
            options = s3bake.BakeOptions(
                frame_end=min(anim.frame_end, 4),
                position_mode=args.pos_mode,
                rotation_mode=args.rot_mode,
            )
            for off in s3diag.fk_check(anim, skeleton, options)[:8]:
                print("    СДВИГ %-24s %6.2f м (кость %.3f м)" % (off.bone, off.max_shift, off.rest_len))
        return

    if args.check:
        bl.set_quaternion_mode(arm)
        skeleton = bl.skeleton_from_armature(arm)
        from io_s3_clip_fbx.s3clip import bake as s3bake

        for path in files:
            try:
                anim, order = read_best(path, skeleton, args)
            except Exception as exc:  # noqa: BLE001
                print("  ОШИБКА %s: %s" % (os.path.basename(path), exc))
                continue
            result = s3bake.bake_clip(anim, skeleton, s3bake.BakeOptions(dense=False))
            print(
                "  %-38s имя: %-28s %3d..%-3d (%2.0f fps) костей: %3d без пары: %d"
                % (
                    os.path.basename(path)[:38],
                    (anim.name or "-")[:28],
                    result.frame_start,
                    result.frame_end,
                    anim.fps,
                    result.matched_bones,
                    len(result.unmatched_hashes),
                )
            )
            for key in result.unmatched_hashes[:5]:
                print("      нет кости для ключа %08X" % key)
            # единицы «только движения»: если множитель != 1, трансляции из клипа
            # масштабируются под риг — именно так могут «уезжать» мелкие кости лица
            try:
                opts = s3bake.BakeOptions(
                    positions=args.positions,
                    positions_root_bone=args.positions_root,
                    motion_ref=args.motion_ref,
                    motion_ref_frame=args.motion_ref_frame,
                    motion_unit_source=args.unit_source,
                    motion_unit_target=args.unit_target,
                    motion_scale=args.motion_scale,
                )
                summ = s3bake.motion_summary(anim, skeleton, opts)
                face = [n for n in summ["moving"]
                        if any(k in n.lower() for k in ("lip", "jaw", "mouth", "tongue"))]
                print("      единицы: клип %.3f, риг %.3f, множитель трансляций %.4f"
                      % (summ["unit_source"], summ["unit_target"], summ["factor"]))
                if face:
                    print("      лицо двигается трансляцией: %s" % ", ".join(sorted(face)[:8]))
            except Exception as exc:  # noqa: BLE001 — диагностика не должна ронять прогон
                print("      (единицы не посчитались: %s)" % exc)
        return

    job = batch_mod.BatchJob(
        out_dir=os.path.abspath(os.path.expanduser(args.output)),
        mode=args.mode,
        format=args.format,
        recursive=not args.no_recursive,
        name_template=args.template,
        single_file_name=args.file_name,
        skip_existing=not args.overwrite,
        dense=not args.sparse,
        interpolation="CONSTANT" if args.constant else "LINEAR",
        missing_position="absolute" if args.absolute_missing_pos else "relative",
        positions=args.positions,
        positions_root_bone=args.positions_root,
        motion_ref=args.motion_ref,
        motion_ref_frame=args.motion_ref_frame,
        motion_unit_source=args.unit_source,
        motion_unit_target=args.unit_target,
        motion_scale=args.motion_scale,
        motion_horizontal=args.motion_horizontal,
        motion_service=args.motion_service,
        ground_snap=not args.no_ground_snap,
        scale=args.scale,
        include_meshes=args.meshes,
        add_leaf_bones=args.leaf_bones,
        position_mode=args.pos_mode,
        rotation_mode=args.rot_mode,
        auto_convention=not args.no_auto,
        all_bones=not args.animated_only,
        deform_bones_only=args.deform_bones_only,
        keep_actions=args.keep_actions,
    )
    if args.chunk_size and args.chunk_size > 0 and args.mode == "single":
        # Библиотеку на тысячи клипов держим кусками: один .glb на N клипов.
        # В Godot это N AnimationLibrary, которые складываются в один AnimationPlayer.
        chunks = [files[i:i + args.chunk_size] for i in range(0, len(files), args.chunk_size)]
        print("[S3 Clip→FBX] библиотека: %d файл(ов) по %d клипов" % (len(chunks), args.chunk_size))
        base_name, ext = os.path.splitext(job.single_file_name)
        total_ok = 0
        total_fail = 0
        elapsed = 0.0
        for index, chunk in enumerate(chunks, start=1):
            job.single_file_name = "%s_%02d%s" % (base_name, index, ext)
            part = batch_mod.run_batch(bpy.context, arm, chunk, job, log=lambda m: print(m))
            total_ok += part.ok_count
            total_fail += len(part.failed)
            elapsed += part.elapsed
        print("\n[S3 Clip→FBX] готово за %.1f с: %d ок, %d с ошибкой, файлов %d"
              % (elapsed, total_ok, total_fail, len(chunks)))
        return

    summary = batch_mod.run_batch(bpy.context, arm, files, job, log=lambda m: print(m))

    print(
        "\n[S3 Clip→FBX] готово за %.1f с: %d ок, %d с ошибкой"
        % (summary.elapsed, summary.ok_count, len(summary.failed))
    )
    if summary.output:
        print("  файл: %s" % summary.output)
    for entry in summary.failed:
        print("  ! %s — %s" % (os.path.basename(entry.path), entry.error))
    for warning in summary.warnings[:20]:
        print("  . %s" % warning)


main()
