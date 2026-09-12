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

"""Thin compatibility layer over RenderDoc's python module.

RenderDoc reworked how bindings are exposed around v1.34: ``ShaderBindpointMapping``
and ``PipeState.GetBindpointMapping()`` went away, ``GetConstantBuffer()`` became
``GetConstantBlock()`` returning a ``UsedDescriptor``, ``GetReadOnlyResources()``
now returns a flat list of descriptors, and ``ConstantBlock.bindPoint`` was
dropped. This module papers over the difference so that the scraper works with
both the older releases people may still have installed and current ones
(tested against the API surface documented for 1.31 through 1.46).

It must stay importable outside of Blender: it is used by the extraction
subprocess, not by the add-on itself.
"""

import renderdoc as rd

# -----------------------------------------------------------------------------
# Feature detection. Done on the class rather than on instances so that it
# happens once, at import time.

HAS_DESCRIPTOR_API = hasattr(rd.PipeState, "GetConstantBlock")
HAS_BINDPOINT_MAPPING = hasattr(rd.PipeState, "GetBindpointMapping")

# -----------------------------------------------------------------------------

def initialiseReplay():
    """Must be called once before any other API call when using the module
    standalone (the UI does it for us when running inside RenderDoc)."""
    if hasattr(rd, "InitialiseReplay"):
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])

def shutdownReplay():
    if hasattr(rd, "ShutdownReplay"):
        rd.ShutdownReplay()

# -----------------------------------------------------------------------------

def getConstantBufferBinding(state, stage, index):
    """Buffer backing the constant block #index of the given shader stage.
    @return (resourceId, byteOffset, byteSize)"""
    if HAS_DESCRIPTOR_API:
        descriptor = state.GetConstantBlock(stage, index, 0).descriptor
        return descriptor.resource, descriptor.byteOffset, descriptor.byteSize
    cbuff = state.GetConstantBuffer(stage, index, 0)
    return cbuff.resourceId, cbuff.byteOffset, cbuff.byteSize

def getConstantBlockSlot(constant_block, index):
    """The `cbufslot` to hand to GetCBufferVariableContents for a given
    constant block. Older versions want the block's bind point, newer ones
    index the reflection's constantBlocks list directly."""
    bind_point = getattr(constant_block, "bindPoint", None)
    return index if bind_point is None else bind_point

def getLastReadOnlyResource(state, stage):
    """Resource id of the last read-only resource (i.e. texture) bound to the
    given stage, or None. The Maps shaders bind the colour texture last, which
    is the heuristic this add-on has always relied on."""
    if HAS_DESCRIPTOR_API:
        used = state.GetReadOnlyResources(stage, True)
        if not used:
            return None
        return used[-1].descriptor.resource

    if HAS_BINDPOINT_MAPPING:
        bindpoints = state.GetBindpointMapping(stage)
        if not bindpoints.samplers:
            return None
        bind = bindpoints.samplers[-1].bind
        resources = state.GetReadOnlyResources(stage)
        if bind >= len(resources) or not resources[bind].resources:
            return None
        return resources[bind].resources[0].resourceId

    resources = state.GetReadOnlyResources(stage)
    if not resources:
        return None
    last = resources[-1]
    # Old-style BoundResourceArray, just in case.
    if hasattr(last, "resources"):
        return last.resources[0].resourceId if last.resources else None
    return last.descriptor.resource

# -----------------------------------------------------------------------------

# rd.VarType.Int was renamed rd.VarType.SInt long ago; keep both working.
_SINT = getattr(rd.VarType, "SInt", None) or getattr(rd.VarType, "Int", None)

def shaderVariableValue(var):
    """Plain python value of a ShaderVariable, or None for types we have no
    use for (the Maps shaders only ever hand us floats and ints)."""
    count = var.rows * var.columns
    if var.type == rd.VarType.Float:
        return var.value.f32v[:count]
    if _SINT is not None and var.type == _SINT:
        return var.value.s32v[:count]
    if var.type == rd.VarType.UInt:
        return var.value.u32v[:count]
    if hasattr(rd.VarType, "Double") and var.type == rd.VarType.Double:
        return var.value.f64v[:count]
    return None

# -----------------------------------------------------------------------------

def constantBlockVariableNames(reflection):
    """Names of every constant declared by a shader, straight from the
    reflection data. Much cheaper than reading the buffers back, which makes it
    usable as a per-drawcall filter."""
    names = set()
    if reflection is None:
        return names
    for block in reflection.constantBlocks:
        for var in block.variables:
            names.add(var.name)
    return names

# -----------------------------------------------------------------------------

def isDrawcall(action):
    return bool(action.flags & rd.ActionFlags.Drawcall)

def isIndexedDrawcall(action):
    return bool(action.flags & rd.ActionFlags.Drawcall) and bool(
        action.flags & rd.ActionFlags.Indexed
    )

def isClear(action):
    return bool(action.flags & rd.ActionFlags.Clear)

# -----------------------------------------------------------------------------

class Drawcall:
    """An ActionDescription plus its resolved name.

    The scraper used to `setattr(action, 'name', ...)` on the RenderDoc object,
    which only works as long as the SWIG bindings allow arbitrary attributes.
    Wrapping is both safer and clearer about what is ours and what is
    RenderDoc's.
    """

    __slots__ = ("action", "name")

    def __init__(self, action, name):
        self.action = action
        self.name = name

    def __getattr__(self, attr):
        return getattr(self.action, attr)

    def __repr__(self):
        return "<Drawcall #{} {}>".format(self.action.eventId, self.name)
