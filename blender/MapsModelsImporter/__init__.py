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

# Blender 4.2 and above read the metadata from blender_manifest.toml instead,
# this block is only used when the add-on is installed the legacy way.
bl_info = {
    "name": "Maps Models Importer",
    "author": "Elie Michel",
    "version": (0, 8, 0),
    "blender": (4, 2, 0),
    "location": "File > Import > Google Maps Capture",
    "description": "Import meshes from a Google Maps or Google Earth capture",
    "warning": "",
    "doc_url": "https://github.com/eliemichel/MapsModelsImporter",
    "tracker_url": "https://github.com/eliemichel/MapsModelsImporter/issues",
    "category": "Import-Export",
}

import bpy

if bpy.app.version < (4, 2, 0):
    raise RuntimeError(
        "Maps Models Importer 0.8+ requires Blender 4.2 or above "
        "(this is Blender {}.{}.{}). Use version 0.7.0 of the add-on for "
        "older versions of Blender.".format(*bpy.app.version)
    )

from . import preferences
from . import properties
from . import operators
from . import panels

modules = (
    preferences,
    properties,
    operators,
    panels,
)

def register():
    for m in modules:
        m.register()

def unregister():
    for m in reversed(modules):
        m.unregister()

if __name__ == "__main__":
    register()
