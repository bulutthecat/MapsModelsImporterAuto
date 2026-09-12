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

"""Import a RenderDoc capture into Blender, headless.

Meant to be run as::

    blender --background --python mmi_blender.py -- --capture foo.rdc --blend foo.blend

`tools/mmi import` does that for you.
"""

import argparse
import os
import sys

import addon_utils
import bpy

ADDON_NAMES = (
    "bl_ext.user_default.maps_models_importer",
    "bl_ext.system.maps_models_importer",
    "MapsModelsImporter",
)


def findAddonModule():
    """Module name of the add-on, however it happens to be installed."""
    available = {module.__name__ for module in addon_utils.modules()}
    for name in ADDON_NAMES:
        if name in available:
            return name
    for name in sorted(available):
        if name.endswith("maps_models_importer") or name.endswith("MapsModelsImporter"):
            return name
    return None


def enableAddon():
    name = findAddonModule()
    if name is None:
        raise SystemExit(
            "The Maps Models Importer add-on is not installed in this Blender.\n"
            "Run `tools/mmi mount` first."
        )
    # default_set=True is what adds the entry to preferences.addons, which is
    # where the add-on's own preferences live. Nothing is written to disk: we
    # run with --factory-startup and never save the user preferences.
    if addon_utils.enable(name, default_set=True, persistent=False) is None:
        raise SystemExit(f"Blender could not enable the add-on module '{name}'")
    print(f"Using add-on module '{name}'")
    return name


def parseArgs(argv):
    parser = argparse.ArgumentParser(
        prog="blender --background --python mmi_blender.py --",
        description="Import a RenderDoc capture of Google Maps into Blender",
    )
    parser.add_argument("--capture", required=True, help="The .rdc file to import")
    parser.add_argument("--blend", default=None, help="Where to save the resulting .blend")
    parser.add_argument("--glb", default=None, help="Where to also export a .glb")
    parser.add_argument(
        "--max-blocks",
        type=int,
        default=-1,
        help="Maximum number of draw calls to import, -1 for no limit",
    )
    parser.add_argument(
        "--experimental",
        action="store_true",
        help="Use the experimental draw call extraction",
    )
    parser.add_argument(
        "--tmp-dir",
        default=None,
        help="Directory for intermediate files and textures",
    )
    parser.add_argument(
        "--python-exe",
        default=None,
        help="Interpreter able to import the renderdoc module",
    )
    parser.add_argument(
        "--renderdoc-module-dir",
        default=None,
        help="Directory containing the renderdoc python module",
    )
    parser.add_argument(
        "--keep-default-scene",
        action="store_true",
        help="Keep Blender's startup cube, camera and light",
    )
    return parser.parse_args(argv)


def clearScene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = parseArgs(argv)

    capture = os.path.abspath(args.capture)
    if not os.path.isfile(capture):
        raise SystemExit(f"No such capture file: {capture}")

    module_name = enableAddon()

    if not args.keep_default_scene:
        clearScene()
        # read_factory_settings resets the add-on list too.
        addon_utils.enable(module_name, default_set=True, persistent=False)

    preferences = bpy.context.preferences.addons[module_name].preferences
    if args.tmp_dir:
        os.makedirs(args.tmp_dir, exist_ok=True)
        preferences.tmp_dir = args.tmp_dir
    if args.python_exe:
        preferences.python_exe = args.python_exe
    if args.renderdoc_module_dir:
        preferences.renderdoc_module_dir = args.renderdoc_module_dir
    preferences.debug_info = True

    print(f"Importing {capture}...")
    result = bpy.ops.import_rdc.google_maps(
        filepath=capture,
        max_blocks=args.max_blocks,
        use_experimental=args.experimental,
    )
    if 'CANCELLED' in result:
        raise SystemExit("The import was cancelled.")

    object_count = len(bpy.context.scene.objects)
    if object_count == 0:
        raise SystemExit(
            "Nothing was imported. The capture probably does not contain the "
            "Google Maps 3D draw calls: make sure you were moving in the 3D "
            "view when you took it."
        )
    print(f"Imported {object_count} objects.")

    if args.blend:
        blend = os.path.abspath(args.blend)
        os.makedirs(os.path.dirname(blend), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=blend)
        print(f"Saved {blend}")

    if args.glb:
        glb = os.path.abspath(args.glb)
        os.makedirs(os.path.dirname(glb), exist_ok=True)
        bpy.ops.export_scene.gltf(filepath=glb, export_format='GLB')
        print(f"Exported {glb}")


if __name__ == "__main__":
    main()
