# Copyright (c) 2019 - 2026 Elie Michel
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

import bpy

addon_idname = __package__

# -----------------------------------------------------------------------------

def getPreferences(context):
    preferences = context.preferences
    addon_preferences = preferences.addons[addon_idname].preferences
    return addon_preferences

# -----------------------------------------------------------------------------

class MapsModelsAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = addon_idname

    tmp_dir: bpy.props.StringProperty(
        name="Temporary Directory",
        subtype='DIR_PATH',
        default="",
        )

    debug_info: bpy.props.BoolProperty(
        name="Debug Info",
        default=False,
        )

    # The RenderDoc python module cannot be loaded from Blender's embedded
    # interpreter, so the capture is read by a separate process. That process
    # does not have to be Blender's python: any interpreter that can import
    # both `renderdoc` and `numpy` will do. This matters a lot on Linux, where
    # the renderdoc module has to be built from source and is therefore tied to
    # whichever python version it was built against.
    python_exe: bpy.props.StringProperty(
        name="Python Executable",
        description=(
            "Python interpreter used to read the capture file. It must be able "
            "to import the renderdoc and numpy modules. Leave empty to use "
            "Blender's own interpreter, or the one advertised by the "
            "MAPSMODELSIMPORTER_PYTHON environment variable"
        ),
        subtype='FILE_PATH',
        default="",
        )

    renderdoc_module_dir: bpy.props.StringProperty(
        name="RenderDoc Module Directory",
        description=(
            "Directory containing the renderdoc python module (renderdoc.pyd "
            "on Windows, renderdoc.so on Linux). Leave empty to use the "
            "binaries shipped with the add-on, or the ones advertised by the "
            "MAPSMODELSIMPORTER_RENDERDOC_MODULE_DIR environment variable"
        ),
        subtype='DIR_PATH',
        default="",
        )

    def draw(self, context):
        layout = self.layout

        col = layout.column(align=True)
        col.label(text="The temporary directory is used for intermediate files and for textures.")
        col.label(text="It can get heavy. If left empty, the capture file's directory is used.")
        layout.prop(self, "tmp_dir")

        col = layout.column(align=True)
        col.label(text="Advanced: where to find RenderDoc's python module, and which")
        col.label(text="interpreter to load it with. Both are optional, see the documentation.")
        layout.prop(self, "renderdoc_module_dir")
        layout.prop(self, "python_exe")

        layout.label(text="Turn on extra debug info:")
        layout.prop(self, "debug_info")

# -----------------------------------------------------------------------------

classes = (MapsModelsAddonPreferences,)

register, unregister = bpy.utils.register_classes_factory(classes)
