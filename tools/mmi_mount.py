# Copyright (c) 2026 Elie Michel
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the “Software”), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# The Software is provided “as is”, without warranty of any kind, express or
# implied, including but not limited to the warranties of merchantability,
# fitness for a particular purpose and noninfringement. In no event shall
# the authors or copyright holders be liable for any claim, damages or other
# liability, whether in an action of contract, tort or otherwise, arising from,
# out of or in connection with the software or the use or other dealings in the
# Software.
#
# This file is part of MapsModelsImporter, a set of addons to import 3D models
# from Maps services

"""Blender-side half of `mmi mount`: enable the add-on and configure it.

Run as::

    blender --background --python mmi_mount.py -- [--python-exe ...] [--disable]
"""

import argparse
import sys

import addon_utils
import bpy

ADDON_NAMES = (
    "bl_ext.user_default.maps_models_importer",
    "bl_ext.system.maps_models_importer",
    "MapsModelsImporter",
)


def findAddonModule():
    available = {module.__name__ for module in addon_utils.modules(refresh=True)}
    for name in ADDON_NAMES:
        if name in available:
            return name
    for name in sorted(available):
        if name.endswith("maps_models_importer") or name.endswith("MapsModelsImporter"):
            return name
    return None


def parseArgs(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-exe", default=None)
    parser.add_argument("--renderdoc-module-dir", default=None)
    parser.add_argument("--tmp-dir", default=None)
    parser.add_argument("--disable", action="store_true")
    return parser.parse_args(argv)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = parseArgs(argv)

    # Make sure Blender notices an extension directory that appeared behind
    # its back (which is exactly what `mmi mount` does).
    try:
        bpy.ops.extensions.repo_refresh_all()
    except (AttributeError, RuntimeError):
        pass

    name = findAddonModule()
    if name is None:
        print("MMI_RESULT=not-found")
        raise SystemExit(1)

    if args.disable:
        addon_utils.disable(name, default_set=True)
        bpy.ops.wm.save_userpref()
        print(f"MMI_RESULT=disabled {name}")
        return

    addon_utils.enable(name, default_set=True, persistent=True)

    preferences = bpy.context.preferences.addons[name].preferences
    if args.python_exe is not None:
        preferences.python_exe = args.python_exe
    if args.renderdoc_module_dir is not None:
        preferences.renderdoc_module_dir = args.renderdoc_module_dir
    if args.tmp_dir is not None:
        preferences.tmp_dir = args.tmp_dir

    bpy.ops.wm.save_userpref()
    print(f"MMI_RESULT=enabled {name}")


if __name__ == "__main__":
    main()
