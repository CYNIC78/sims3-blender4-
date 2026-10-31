"""S3 Clip → FBX — аддон для Blender 4.x/5.x.

Читает анимации The Sims 3 (.animation / CLIP) и умеет:
  • импортировать клип как экшен на риге Sims 3;
  • пакетно конвертировать папку клипов в отдельные .fbx (или один FBX с NLA-треками);
  • переключать имена костей b__L_… ⇄ .L/.R для симметричной правки весов;
  • автоопределять конвенцию данных (абсолютные значения / дельта от rest) и
    диагностировать клип, если поза «взорвалась»;
  • работать из командной строки: blender -b rig.blend --python s3clip_cli.py -- ...

Формат CLIP: спецификация SimsWiki (Sims 3:0x6B20C4F3) и s3py.
"""

bl_info = {
    "name": "Sims 3 Clip → FBX",
    "author": "Arena.ai",
    "version": (1, 9, 4),
    "blender": (3, 6, 0),
    "location": "N-панель «S3 Clip» / File ▸ Import",
    "description": "Импорт анимаций The Sims 3 (.animation) и пакетный экспорт в FBX",
    "category": "Import-Export",
}

import importlib
import sys

# ui импортируется лениво: ядро (s3clip) должно работать без bpy —
# это нужно для тестов обычным Python и для CLI-разбора файлов.


def _reload_modules():
    """Перезагрузка подмодулей — удобно при правках изнутри Blender."""
    package = __name__
    for name in list(sys.modules):
        if name.startswith(package + ".") and name not in (package + ".ui",):
            try:
                importlib.reload(sys.modules[name])
            except Exception:
                pass
    if package + ".ui" in sys.modules:
        importlib.reload(sys.modules[package + ".ui"])


def register():
    from . import ui

    _reload_modules()
    ui.register()


def unregister():
    from . import ui

    ui.unregister()


if __name__ == "__main__":
    register()
