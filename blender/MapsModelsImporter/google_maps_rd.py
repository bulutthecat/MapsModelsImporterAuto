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

MSG_RD_IMPORT_FAILED = """Error: Failed to load the RenderDoc Module. It however seems to exist.
This might be due to one of the following reasons:
 - The python running this script is not the version the RenderDoc Module was built against
 - An additional file required by the RenderDoc Module is missing (i.e. renderdoc.dll on
   Windows, librenderdoc.so on Linux)
 - Something completely different

Remember, you must use exactly the same version of python to load the RenderDoc Module as was used to build it.
Find more information about building the RenderDoc Module here: https://github.com/baldurk/renderdoc/blob/v1.x/docs/CONTRIBUTING/Compiling.md\n"""

import os
import re
import sys
import pickle
import numpy as np

# The name-based scraping strategies are extremely chatty; keep their output
# behind a switch so that the debug log stays readable.
VERBOSE = bool(os.environ.get("MAPSMODELSIMPORTER_VERBOSE"))

try:
    import renderdoc as rd
except ModuleNotFoundError as err:
    print("Error: Can't find the RenderDoc Module.")
    print("sys.path contains the following paths:\n")
    print(*sys.path, sep = "\n")
    sys.exit(20)
except ImportError as err:
    print(MSG_RD_IMPORT_FAILED)
    print("sys.platform: ", sys.platform)
    print("Python version: ",sys.version)
    print("err.name: ",err.name)
    print("err.path: ",err.path)
    print("Error Message: ", err,"\n")
    sys.exit(21)

import bcdecode
import rdcompat
import rdtexfile
from rdcompat import Drawcall
from meshdata import MeshData, makeMeshData
from profiling import Timer, profiling_counters
from rdutils import CaptureWrapper

_, CAPTURE_FILE, FILEPREFIX, MAX_BLOCKS_STR = sys.argv[:4]
MAX_BLOCKS = int(MAX_BLOCKS_STR)

# Uniforms that identify the vertex shader drawing the 3D tiles, per capture
# type. These names are what the various services' shaders end up with once
# they have gone through the browser's shader translator.
CAPTURE_TYPE_UNIFORMS = {
    "Google Maps": ("_w", "_s"),
    "Google Earth": ("_uMeshToWorldMatrix",),
    "Mapy CZ": ("_uMV", "_uParams"),
}

# ANGLE hashes the uniform names of WebGL shaders, which is why name matching
# is hopeless for anything rendered by a browser.
ANGLE_HASHED_NAME = re.compile(r"^_?webgl_[0-9a-f]{16}$")

# What decodeTextureOurselves() returns when the texture is empty everywhere.
BLANK = "blank"

def isAngleWebglShader(names):
    return any(ANGLE_HASHED_NAME.match(name) for name in names)

def classifyConstants(constants):
    """Split a shader's constants into 4x4 matrices and 4-component vectors,
    the two things the importer needs."""
    matrices = [name for name, rows, columns in constants if rows == 4 and columns == 4]
    vectors = [name for name, rows, columns in constants if rows == 1 and columns == 4]
    return matrices, vectors

def hasPositionAndUV(attributes):
    """Whether a vertex layout looks like textured geometry: something with at
    least 3 components to be a position, and something with 2 to be a UV."""
    counts = [count for _, count in attributes]
    return len(counts) >= 2 and any(c >= 3 for c in counts) and any(c == 2 for c in counts)

def captureApi(state):
    """Which graphics API the capture was taken with. It matters for matrices:
    HLSL shaders multiply row vectors (mul(pos, M)), GLSL ones column vectors
    (M * pos), so the same 16 numbers mean transposed things."""
    for name, check in (("GL", "IsCaptureGL"), ("VK", "IsCaptureVK"),
                        ("D3D11", "IsCaptureD3D11"), ("D3D12", "IsCaptureD3D12")):
        probe = getattr(state, check, None)
        if probe is not None and probe():
            return name
    return "unknown"

# Below this a PNG is a flat colour, i.e. not a tile's texture.
MIN_TEXTURE_BYTES = 2048

def numpySave(array, file):
    np.array([array.ndim], dtype=np.int32).tofile(file)
    np.array(array.shape, dtype=np.int32).tofile(file)
    dt = array.dtype.descr[0][1][1:3].encode('ascii')
    file.write(dt)
    array.tofile(file)

class CaptureScraper():
    def __init__(self, controller, filename=None):
        self.controller = controller
        # The capture file itself, for texture contents the replay does not
        # hand back. Parsed on first need only.
        self.file_textures = rdtexfile.CaptureFileTextures(filename) if filename else None
        self._uniform_names_cache = {}
        self._signature_cache = {}
        # Names the structural strategy worked out, handed to the importer so
        # that it knows which constant is the matrix and which the UV
        # transform when it cannot recognise them by name.
        self.uniform_hints = None
        self._textures = None
        self.texture_report = {"saved": 0, "missing": 0, "blank": 0, "choices": set(),
                               "from_file": 0}
        self.capture_api = "unknown"
        # (events, draw calls, indexed draw calls) seen while scraping, used to
        # explain what went wrong when nothing relevant was found.
        self.capture_summary = None

    def findDrawcallBatch(self, drawcalls, first_call_prefix, drawcall_prefix, last_call_prefix):
        batch = []
        has_batch_started = False
        last_call_index = 0
        for last_call_index, draw in enumerate(drawcalls):
            if has_batch_started:
                if not draw.name.startswith(drawcall_prefix):
                    if draw.name.startswith(last_call_prefix) and batch != []:
                        break
                    else:
                        if VERBOSE:
                            print("(Skipping drawcall {})".format(draw.name))
                        continue
                batch.append(draw)
            elif draw.name.startswith(first_call_prefix):
                has_batch_started = True
                if draw.name.startswith(drawcall_prefix):
                    batch.append(draw)
            elif VERBOSE:
                print(f"Not relevant yet: {draw.name}")
        return batch, last_call_index

    def getVertexShaderConstants(self, draw, state=None):
        controller = self.controller
        if state is None:
            controller.SetFrameEvent(draw.eventId, True)
            state = controller.GetPipelineState()

        shader = state.GetShader(rd.ShaderStage.Vertex)
        ep = state.GetShaderEntryPoint(rd.ShaderStage.Vertex)
        ref = state.GetShaderReflection(rd.ShaderStage.Vertex)
        constants = {}
        if ref is None:
            return constants
        for cbn, cb in enumerate(ref.constantBlocks):
            block = {}
            resource_id, offset, size = rdcompat.getConstantBufferBinding(
                state, rd.ShaderStage.Vertex, cbn
            )
            variables = controller.GetCBufferVariableContents(
                state.GetGraphicsPipelineObject(),
                shader,
                rd.ShaderStage.Vertex,
                ep,
                rdcompat.getConstantBlockSlot(cb, cbn),
                resource_id,
                offset,
                size
            )
            for var in variables:
                if var.members:
                    val = []
                    for member in var.members:
                        memval = rdcompat.shaderVariableValue(member)
                        if memval is None:
                            print(f"Unsupported type for {cb.name}.{var.name}.{member.name}!")
                            memval = 0
                        val.append(memval)
                else:
                    val = rdcompat.shaderVariableValue(var)
                    if val is None:
                        print(f"Unsupported type for {cb.name}.{var.name}!")
                        val = 0
                block[var.name] = val
            constants[cb.name] = block
        return constants

    def getVertexShaderSignature(self, draw):
        """What a draw call's vertex shader looks like, without reading any
        buffer back: the shader's id, the constants it declares with their
        dimensions, and the shape of its vertex inputs. Enough to recognise
        the map geometry without knowing a single uniform name."""
        cached = self._signature_cache.get(draw.eventId)
        if cached is not None:
            return cached

        self.controller.SetFrameEvent(draw.eventId, False)
        state = self.controller.GetPipelineState()
        reflection = state.GetShaderReflection(rd.ShaderStage.Vertex)

        constants = []
        if reflection is not None:
            for block in reflection.constantBlocks:
                for var in block.variables:
                    constants.append((var.name, var.type.rows, var.type.columns))

        attributes = []
        for attr in state.GetVertexInputs():
            attributes.append((attr.name, attr.format.compCount))

        signature = {
            "shader": str(state.GetShader(rd.ShaderStage.Vertex)),
            "constants": constants,
            "attributes": attributes,
        }
        self._signature_cache[draw.eventId] = signature
        return signature

    def getVertexUniformNames(self, draw):
        """Names of the constants declared by a draw call's vertex shader.
        Read from the reflection data only, so no buffer contents are fetched:
        this is cheap enough to run over every draw call of a capture."""
        cached = self._uniform_names_cache.get(draw.eventId)
        if cached is not None:
            return cached
        names = {name for name, _, _ in self.getVertexShaderSignature(draw)["constants"]}
        self._uniform_names_cache[draw.eventId] = names
        return names

    def hasUniform(self, draw, uniform):
        return uniform in self.getVertexUniformNames(draw)

    def detectCaptureType(self, draw):
        """Which service, if any, a draw call belongs to, from the uniforms its
        vertex shader declares."""
        names = self.getVertexUniformNames(draw)
        for capture_type, uniforms in CAPTURE_TYPE_UNIFORMS.items():
            if all(u in names for u in uniforms):
                return capture_type
        # The Google Maps shaders come in a couple of flavours depending on the
        # browser's shader translator.
        if "webgl_3c7b7f37a9bd4c1d" in names or "_webgl_3c7b7f37a9bd4c1d" in names:
            return "Google Maps"
        return None

    def extractRelevantCalls(self, drawcalls, _strategy=0):
        """List the drawcalls related to drawing the 3D meshes thank to a ad hoc heuristic
        It may different in RenderDoc UI and in Python module, for some reason
        """
        first_call = ""
        last_call = "glDrawArrays(4)"
        drawcall_prefix = "glDrawElements"
        min_drawcall = 0
        capture_type = "Google Maps"
        if _strategy == 0:
            first_call = "glClear(Color = <0.000000, 0.000000, 0.000000, 1.000000>, Depth = <1.000000>)"
        elif _strategy == 1:
            first_call = "glClear(Color = <0.000000, 0.000000, 0.000000, 1.000000>, Depth = <1.000000>, Stencil = <0x00>)"
        elif _strategy == 2:
            first_call = "glClear(Color = <0.000000, 0.000000, 0.000000, 1.000000>, Depth = <0.000000>)"
        elif _strategy == 3:
            first_call = "glClear(Color = <0.000000, 0.000000, 0.000000, 1.000000>, Depth = <0.000000>, Stencil = <0x00>)"
        elif _strategy == 4:
            first_call = ""
            last_call = "ClearDepthStencilView"
            drawcall_prefix = "DrawIndexed"
            capture_type = "Mapy CZ"
        elif _strategy == 5:
            # With Google Earth there are two batches of DrawIndexed calls, we are interested in the second one
            first_call = "DrawIndexed"
            last_call = ""
            drawcall_prefix = "DrawIndexed"
            capture_type = "Google Earth"
            min_drawcall = 0
            while True:
                skipped_drawcalls, new_min_drawcall = self.findDrawcallBatch(drawcalls[min_drawcall:], first_call, drawcall_prefix, last_call)
                if not skipped_drawcalls or self.hasUniform(skipped_drawcalls[0], "_uMeshToWorldMatrix"):
                    break # Found a good draw call
                min_drawcall += new_min_drawcall
        elif _strategy == 6:
            # Actually sometimes there's only one batch
            first_call = "DrawIndexed"
            last_call = ""
            drawcall_prefix = "DrawIndexed"
            capture_type = "Google Earth (single)"
        elif _strategy == 7:
            first_call = "ClearRenderTargetView(0.000000, 0.000000, 0.000000"
            last_call = "Draw()"
            drawcall_prefix = "DrawIndexed"
        elif _strategy == 8:
            first_call = "" # Try from the beginning on
            last_call = "Draw()"
            drawcall_prefix = "DrawIndexed"
            min_drawcall = 0
            while True:
                skipped_drawcalls, new_min_drawcall = self.findDrawcallBatch(drawcalls[min_drawcall:], first_call, drawcall_prefix, last_call)
                if not skipped_drawcalls or self.hasUniform(skipped_drawcalls[0], "_w"):
                    break # Found a good draw call
                min_drawcall += new_min_drawcall
        else:
            # Every name-based strategy failed. Fall back on looking at what the
            # shaders actually declare, which does not care about the graphics
            # API the capture was taken with. This is what makes captures taken
            # on Linux (OpenGL or Vulkan) work, where none of the D3D11 call
            # names above ever show up.
            return self.extractRelevantCallsByUniforms(drawcalls)

        print(f"Trying scraping strategy #{_strategy} (from draw call #{min_drawcall})...")
        relevant_drawcalls, new_min_drawcall = self.findDrawcallBatch(
            drawcalls[min_drawcall:],
            first_call,
            drawcall_prefix,
            last_call)

        if not relevant_drawcalls:
            return self.extractRelevantCalls(drawcalls, _strategy=_strategy+1)

        if capture_type == "Mapy CZ" and not self.hasUniform(relevant_drawcalls[0], "_uMV"):
            return self.extractRelevantCalls(drawcalls, _strategy=_strategy+1)

        if capture_type == "Google Earth (single)":
            if not self.hasUniform(relevant_drawcalls[0], "_uMeshToWorldMatrix"):
                return self.extractRelevantCalls(drawcalls, _strategy=_strategy+1)
            else:
                capture_type = "Google Earth"

        if capture_type == "Google Earth":
            relevant_drawcalls = [
                call for call in relevant_drawcalls
                if self.hasUniform(call, "_uMeshToWorldMatrix")
            ]

        if capture_type == "Google Maps":
            # Accumulate multiple batches
            batch_count = 1
            while True:
                min_drawcall += new_min_drawcall
                # Find a batch
                first_call = "" # Try from the beginning on
                last_call = "Draw()"
                drawcall_prefix = "DrawIndexed"
                while True:
                    skipped_drawcalls, new_min_drawcall = self.findDrawcallBatch(drawcalls[min_drawcall:], first_call, drawcall_prefix, last_call)
                    if not skipped_drawcalls or self.hasUniform(skipped_drawcalls[0], "_w"):
                        break # Found a good draw call
                    min_drawcall += new_min_drawcall

                # Accumulate the batch
                new_relevant_drawcalls, new_min_drawcall = self.findDrawcallBatch(
                    drawcalls[min_drawcall:],
                    first_call,
                    drawcall_prefix,
                    last_call)

                if not new_relevant_drawcalls:
                    break

                relevant_drawcalls.extend(new_relevant_drawcalls)
                batch_count += 1

            print(f"Found {batch_count} batches.")

        return relevant_drawcalls, capture_type

    def extractRelevantCallsByUniforms(self, drawcalls):
        """API-agnostic fallback: keep every indexed draw call whose vertex
        shader declares the uniforms one of the supported services uses.

        Unlike the strategies above this makes no assumption about how the
        driver names its draw calls, so it works the same for D3D11, OpenGL and
        Vulkan captures. It is slower, because it has to look at the pipeline
        state of every draw call, hence its use as a last resort only."""
        print("Trying scraping strategy 'by uniform' (API agnostic)...")

        all_draws = [draw for draw in drawcalls if rdcompat.isDrawcall(draw)]
        candidates = [draw for draw in all_draws if rdcompat.isIndexedDrawcall(draw)]
        print(
            f"Examining {len(candidates)} indexed draw calls "
            f"(out of {len(all_draws)} draw calls, {len(drawcalls)} events)..."
        )

        per_type = {}
        for draw in candidates:
            capture_type = self.detectCaptureType(draw)
            if capture_type is not None:
                per_type.setdefault(capture_type, []).append(draw)

        if not per_type:
            self.capture_summary = (len(drawcalls), len(all_draws), len(candidates))
            print("No shader declares the uniform names we know about.")
            return self.extractRelevantCallsByStructure(drawcalls)

        # If several services matched (they should not), go with the one that
        # accounts for the most geometry.
        capture_type = max(per_type, key=lambda t: len(per_type[t]))
        relevant_drawcalls = per_type[capture_type]
        print(f"Found {len(relevant_drawcalls)} relevant draw calls.")
        return relevant_drawcalls, capture_type

    def extractRelevantCallsByStructure(self, drawcalls):
        """Last resort: recognise the map geometry by the shape of its draw
        calls rather than by the names of its uniforms.

        Chrome renders WebGL through ANGLE, and ANGLE rewrites every uniform
        name to `webgl_<16 hex digits>`, hashed from the original. The hashes
        in extractUniforms() were captured from one version of the Google Maps
        shaders years ago; when Google edits a shader, every hash changes and
        name matching stops working -- even though the capture is perfectly
        good.

        What does not change is the shape: the tiles are drawn by one shader,
        used by far more draw calls than anything else on the page, taking a
        position and a UV attribute, and holding a 4x4 matrix and at least one
        vec4 in its constants.
        """
        print("Trying scraping strategy 'by structure' (name agnostic)...")

        candidates = [draw for draw in drawcalls if rdcompat.isIndexedDrawcall(draw)]
        if not candidates:
            return [], "none"

        groups = {}
        for draw in candidates:
            signature = self.getVertexShaderSignature(draw)
            groups.setdefault(signature["shader"], []).append((draw, signature))

        scored = []
        for shader, entries in groups.items():
            signature = entries[0][1]
            matrices, vectors = classifyConstants(signature["constants"])
            if not matrices or not vectors:
                continue
            if not hasPositionAndUV(signature["attributes"]):
                continue

            # Which of those constants actually change from one draw call to
            # the next? A per-tile placement matrix has to; a camera or
            # projection matrix shared by every tile does not. This is what
            # separates the terrain shader from anything else that also
            # happens to take a matrix, a vec4, a position and a UV -- roads,
            # labels, the browser's own quads -- and, within the terrain
            # shader, the matrix that places the tile from the one that
            # merely projects it. Sampling a handful of draws is enough.
            varying = self.findVaryingConstants([draw for draw, _ in entries])
            varying_matrices = [m for m in matrices if m in varying]
            varying_vectors = [v for v in vectors if v in varying]
            scored.append({
                "count": len(entries),
                "shader": shader,
                "entries": entries,
                "matrices": varying_matrices + [m for m in matrices if m not in varying],
                "vectors": varying_vectors + [v for v in vectors if v not in varying],
                "varying_matrices": varying_matrices,
                "signature": signature,
            })

        if not scored:
            print("No shader looks like it draws textured 3D tiles.")
            return [], "none"

        # Best: a shader whose matrix changes per draw call, used by the most
        # draw calls. A shader whose matrix is the same for every call cannot
        # be placing tiles with it, however many calls it makes.
        scored.sort(key=lambda g: (bool(g["varying_matrices"]), g["count"]), reverse=True)

        print("Candidate shaders (draw calls, per-draw matrices / shared matrices):")
        for group in scored[:6]:
            shared = [m for m in group["matrices"] if m not in group["varying_matrices"]]
            print(f"  - {group['shader']}: {group['count']} calls, "
                  f"varying {group['varying_matrices'] or 'none'}, shared {shared or 'none'}")

        chosen = scored[0]
        signature = chosen["signature"]
        angle = isAngleWebglShader(name for name, _, _ in signature["constants"])
        print(f"Picked shader {chosen['shader']} used by {chosen['count']} indexed draw calls.")
        print(f"  matrix candidates: {chosen['matrices']}")
        print(f"  uv candidates: {chosen['vectors']}")
        if angle:
            print("  (its uniform names are ANGLE hashes, as Chrome's WebGL produces)")
        if not chosen["varying_matrices"]:
            print("  WARNING: none of its matrices changes between draw calls, so the tile")
            print("  placement is not in a matrix. The import will pile every tile onto the")
            print("  same spot. Run `tools/mmi inspect --source` on this capture and report")
            print("  the vertex shader, so that the actual placement formula can be added.")

        self.uniform_hints = {
            "matrix_candidates": chosen["matrices"],
            "uv_candidates": chosen["vectors"],
            "varying_matrices": chosen["varying_matrices"],
        }
        return [draw for draw, _ in chosen["entries"]], "Google Maps"

    def findVaryingConstants(self, draws, samples=8):
        """Names of the constants whose values differ between draw calls, judged
        on a spread of up to `samples` of them."""
        if len(draws) < 2:
            return set()
        step = max(1, len(draws) // samples)
        picked = draws[::step][:samples]
        if len(picked) < 2:
            picked = draws[:2]

        seen = {}
        for draw in picked:
            constants = self.getVertexShaderConstants(draw)
            for block in constants.values():
                if not isinstance(block, dict):
                    continue
                for name, value in block.items():
                    try:
                        key = tuple(round(float(v), 6) for v in value)
                    except (TypeError, ValueError):
                        key = repr(value)
                    seen.setdefault(name, set()).add(key)
        return {name for name, values in seen.items() if len(values) > 1}

    def consolidateEvents(self, rootList, accumulator=None):
        if accumulator is None:
            accumulator = []
        sdfile = self.controller.GetStructuredFile()
        for root in rootList:
            name = root.GetName(sdfile)
            accumulator.append(Drawcall(root, name.split('::', 1)[-1]))
            self.consolidateEvents(root.children, accumulator)
        return accumulator

    def run(self):
        controller = self.controller

        timer = Timer()
        drawcalls = self.consolidateEvents(controller.GetRootActions())
        profiling_counters['consolidateEvents'].add_sample(timer)

        timer = Timer()
        relevant_drawcalls, capture_type = self.extractRelevantCalls(drawcalls)
        profiling_counters['extractRelevantCalls'].add_sample(timer)

        if not relevant_drawcalls:
            raise RuntimeError(
                "Could not find any relevant draw call in this capture."
                + self.explainEmptyCapture()
            )

        print(f"Scraping capture from {capture_type}...")

        # Whichever strategy found the draw calls, work out which constants
        # hold the matrix and the UV transform. The importer needs that
        # whenever it cannot recognise them by name, which is the normal case
        # for anything ANGLE translated.
        if self.uniform_hints is None:
            signature = self.getVertexShaderSignature(relevant_drawcalls[0])
            matrices, vectors = classifyConstants(signature["constants"])
            if matrices and vectors:
                varying = self.findVaryingConstants(relevant_drawcalls)
                self.uniform_hints = {
                    "matrix_candidates": [m for m in matrices if m in varying]
                                         + [m for m in matrices if m not in varying],
                    "uv_candidates": [v for v in vectors if v in varying]
                                     + [v for v in vectors if v not in varying],
                    "varying_matrices": [m for m in matrices if m in varying],
                }

        if MAX_BLOCKS <= 0:
            max_drawcall = len(relevant_drawcalls)
        else:
            max_drawcall = min(MAX_BLOCKS, len(relevant_drawcalls))

        for drawcallId, draw in enumerate(relevant_drawcalls[:max_drawcall]):
            timer = Timer()

            controller.SetFrameEvent(draw.eventId, True)
            state = controller.GetPipelineState()
            self.capture_api = captureApi(state)

            ib = state.GetIBuffer()
            vbs = state.GetVBuffers()
            attrs = state.GetVertexInputs()
            meshes = [makeMeshData(attr, ib, vbs, draw) for attr in attrs]

            try:
                # Position
                m = meshes[0]
                indices = m.fetchIndices(controller)
                with open("{}{:05d}-indices.bin".format(FILEPREFIX, drawcallId), 'wb') as file:
                    numpySave(indices, file)

                unpacked = m.fetchData(controller)
                with open("{}{:05d}-positions.bin".format(FILEPREFIX, drawcallId), 'wb') as file:
                    numpySave(unpacked, file)

                # UV
                uv_index = 2 if capture_type == "Google Earth" else 1
                if len(meshes) <= uv_index:
                    raise Exception("No UV data")
                m = meshes[uv_index]
                unpacked = m.fetchData(controller)
                with open("{}{:05d}-uv.bin".format(FILEPREFIX, drawcallId), 'wb') as file:
                    numpySave(unpacked, file)
            except Exception as err:
                print("(Skipping because of error: {})".format(err))
                continue

            # Vertex Shader Constants
            constants = self.getVertexShaderConstants(draw, state=state)
            constants["DrawCall"] = {
                "topology": 'TRIANGLE_STRIP' if state.GetPrimitiveTopology() == rd.Topology.TriangleStrip else 'TRIANGLES',
                "type": capture_type,
                "uniform_hints": self.uniform_hints,
                "api": captureApi(state),
            }
            with open("{}{:05d}-constants.bin".format(FILEPREFIX, drawcallId), 'wb') as file:
                pickle.dump(constants, file)

            subtimer = Timer()
            self.extractTexture(drawcallId, state, draw.eventId)
            profiling_counters['extractTexture'].add_sample(subtimer)

            profiling_counters['processDrawEvent'].add_sample(timer)

        report = self.texture_report
        print(f"Textures: {report['saved']} saved, {report['blank']} blank, "
              f"{report['missing']} draw calls without one"
              + (f", {report['from_file']} read from the capture file because the "
                 "replay returned them empty" if report['from_file'] else ""))
        if report["saved"] == 0 and max_drawcall > 0:
            print("  No usable texture was found for any tile. Run `tools/mmi inspect")
            print("  --textures` on this capture and report what the shader binds.")
        if report["blank"] and self.file_textures is not None and self.file_textures.error:
            print(f"  (the capture file could not be read for texture data: "
                  f"{self.file_textures.error})")

        if self.file_textures is not None:
            self.file_textures.close()

        print("Profiling counters:")
        for key, counter in profiling_counters.items():
            print(f" - {key}: {counter.summary()}")

    def explainEmptyCapture(self):
        """Turn 'nothing found' into something the user can act on. What the
        capture does contain says a lot about which step went wrong."""
        if self.capture_summary is None:
            return ""
        events, draws, indexed = self.capture_summary

        message = f"\nThe capture holds {events} events, {draws} draw calls, "
        message += f"{indexed} of them indexed.\n"

        if draws == 0:
            message += (
                "No draw call at all: the capture caught a frame in which nothing "
                "was rendered. Take another one while the 3D view is being moved."
            )
        elif indexed == 0:
            message += (
                "None of them is indexed, which is what the map geometry uses. "
                "This usually means the capture caught the browser compositing "
                "its window rather than the page drawing its 3D tiles: the 3D "
                "content is being rendered by a software fallback (SwiftShader) "
                "that RenderDoc does not see. Check chrome://gpu to confirm that "
                "WebGL is hardware accelerated."
            )
        else:
            message += (
                "None of them uses the uniforms Google Maps, Google Earth or "
                "Mapy CZ declare. Make sure the page really is in 3D mode, and "
                "that you were MOVING in the view at the moment of the capture "
                "(Google Maps only streams its geometry while the view moves)."
            )
        return message

    def textureDescriptions(self):
        """resourceId -> TextureDescription for every texture in the capture,
        fetched once."""
        if self._textures is None:
            self._textures = {t.resourceId: t for t in self.controller.GetTextures()}
        return self._textures

    def candidateTextures(self, state):
        """The textures the fragment shader can read, best candidate first.

        The colour texture of a tile is a sizeable 2D image. Anything else the
        shader reads -- a 1x1 placeholder, a lookup table, a depth texture --
        is not, and taking "the last bound resource" (which is what used to
        happen) picks one of those as soon as the shader binds more than one
        texture, and the tile comes out black.
        """
        descriptions = self.textureDescriptions()
        ranked = []
        for position, rid in enumerate(rdcompat.getReadOnlyResources(state, rd.ShaderStage.Fragment)):
            if rid is None or rid == rd.ResourceId.Null():
                continue
            desc = descriptions.get(rid)
            if desc is None:
                ranked.append((0, position, rid, None))
                continue
            if desc.creationFlags & rd.TextureCategory.DepthTarget and not (
                desc.creationFlags & rd.TextureCategory.ShaderRead
            ):
                continue
            if desc.type in (rd.TextureType.Buffer, rd.TextureType.Texture1D,
                             rd.TextureType.Texture3D):
                continue
            ranked.append((desc.width * desc.height, position, rid, desc))
        # Largest first; on a tie, the last bound one, as before.
        ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return ranked

    def extractTexture(self, drawcallId, state, eventId=None):
        """Save the tile's colour texture as a png."""
        candidates = self.candidateTextures(state)
        if not candidates:
            print(f"Warning: No texture found for drawcall {drawcallId}")
            self.texture_report["missing"] += 1
            return

        path = "{}{:05d}-texture.png".format(FILEPREFIX, drawcallId)
        for area, position, rid, desc in candidates:
            # Compressed textures first go through our own decoder: on an
            # OpenGL replay RenderDoc's SaveTexture() reports success for a
            # BC1 texture and writes a black image, while the compressed
            # blocks it hands back are perfectly fine.
            how = self.decodeTextureOurselves(rid, desc, path, eventId=eventId)
            if how is not None and how != BLANK:
                size = os.path.getsize(path)
                self.reportTexture(drawcallId, rid, desc, size, position, len(candidates), how)
                return
            if how == BLANK:
                self.explainBlank(drawcallId, rid, desc)
                continue

            texsave = rd.TextureSave()
            texsave.resourceId = rid
            texsave.mip = 0
            texsave.slice.sliceIndex = 0
            # The colour is all we want. Preserving alpha lets a texture whose
            # alpha channel is unused (and zero) come out fully transparent,
            # which reads as black in Blender.
            texsave.alpha = rd.AlphaMapping.Discard
            texsave.destType = rd.FileType.PNG
            timer = Timer()
            ok = self.controller.SaveTexture(texsave, path)
            profiling_counters["SaveTexture"].add_sample(timer)

            # A real satellite texture does not compress to a few hundred
            # bytes; a uniform (black, transparent) one does. Try the next
            # candidate rather than shipping a blank.
            size = os.path.getsize(path) if os.path.isfile(path) else 0
            threshold = MIN_TEXTURE_BYTES
            if desc is not None:
                # A tiny texture cannot be expected to weigh much: a genuine
                # 4x4 image is under a hundred bytes of PNG.
                threshold = min(MIN_TEXTURE_BYTES, 4 * desc.width * desc.height)
            if ok and size >= threshold:
                self.reportTexture(drawcallId, rid, desc, size, position, len(candidates), "RenderDoc")
                return
            print(f"  texture {rid} for drawcall {drawcallId} looks blank "
                  f"({size} bytes{'' if ok else ', save failed'}), trying another")
            # Last chance for this candidate: read the raw bytes and convert
            # them ourselves, in case it is the save that failed and not the
            # data.
            how = self.decodeTextureOurselves(rid, desc, path, force=True, eventId=eventId)
            if how is not None and how != BLANK:
                size = os.path.getsize(path)
                if size >= threshold:
                    self.reportTexture(drawcallId, rid, desc, size, position, len(candidates), how)
                    return
            if how == BLANK:
                self.explainBlank(drawcallId, rid, desc)

        self.texture_report["blank"] += 1
        # Leave the last attempt in place rather than nothing at all.

    def textureBytesFromFile(self, rid, desc, eventId):
        """The texture's level 0 as stored in the capture file, or None."""
        textures = self.file_textures
        if textures is None:
            return None
        if not textures.loaded:
            timer = Timer()
            ok = textures.load(self.controller)
            profiling_counters["readCaptureFile"].add_sample(timer)
            if not ok:
                print(f"  could not read texture uploads from the capture file: {textures.error}")
        if textures.error:
            return None
        try:
            return textures.levelZero(rid, desc, eventId)
        except Exception as err:  # noqa: BLE001 - the replay data is still there
            print(f"  could not rebuild texture {rid} from the capture file: {err}")
            return None

    def explainBlank(self, drawcallId, rid, desc):
        """Say once why a texture came out empty."""
        if "blank_explained" in self.texture_report:
            return
        self.texture_report["blank_explained"] = True
        what = f"{desc.width}x{desc.height} {desc.format.Name()}" if desc is not None else "?"
        print(f"  texture {rid} ({what}) for drawcall {drawcallId} is all zero in the replay")
        textures = self.file_textures
        if textures is None or textures.error:
            print("   and the capture file could not be consulted for its uploads")
        else:
            print(f"   and the capture file holds for it: {textures.describe(rid)}")
            print("   so RenderDoc never captured what the browser uploaded into it. "
                  "Try `tools/mmi up --api gles`.")

    def decodeTextureOurselves(self, rid, desc, path, force=False, eventId=None):
        """Write the texture as a PNG from its raw bytes.

        Returns how it was done ("decoded from the replay" or "... capture
        file"), BLANK when every source came back all zero (the PNG is still
        written), or None when the format is not one we decode or (unless
        forced) when RenderDoc can be trusted with it.
        """
        if desc is None:
            return None
        fmt = desc.format
        kind = getattr(fmt, "type", None)
        compressed = kind in (rd.ResourceFormatType.BC1, rd.ResourceFormatType.BC2,
                              rd.ResourceFormatType.BC3)
        plain = (kind == rd.ResourceFormatType.Regular and fmt.compByteWidth == 1
                 and fmt.compCount in (1, 2, 3, 4))
        if not compressed and not (force and plain):
            return None

        try:
            raw = self.controller.GetTextureData(rid, rd.Subresource(0, 0, 0))
            how = "our decoder, from the replay"
            if rdtexfile.isAllZero(raw):
                # The replay has nothing in this texture. The capture file
                # still records what was uploaded into it, so use that.
                alt = self.textureBytesFromFile(rid, desc, eventId)
                if alt is not None and not rdtexfile.isAllZero(alt):
                    raw = alt
                    how = "our decoder, from the capture file"
                    self.texture_report["from_file"] += 1
                else:
                    how = BLANK
            if kind == rd.ResourceFormatType.BC1:
                image = bcdecode.decodeBC1(raw, desc.width, desc.height)
            elif kind == rd.ResourceFormatType.BC2:
                image = bcdecode.decodeBC2(raw, desc.width, desc.height)
            elif kind == rd.ResourceFormatType.BC3:
                image = bcdecode.decodeBC3(raw, desc.width, desc.height)
            else:
                image = bcdecode.decodeRGBA8(raw, desc.width, desc.height,
                                             fmt.compCount, fmt.BGRAOrder())
        except (ValueError, RuntimeError) as err:
            print(f"  could not decode {fmt.Name()} texture {rid} ourselves: {err}")
            return None

        # RenderDoc writes OpenGL textures top row first, as PNGs are; the raw
        # bytes come in OpenGL's own order, bottom row first. Match RenderDoc,
        # which is what the UVs were tuned against.
        if self.capture_api in ("GL", "GLES"):
            image = image[::-1]
        bcdecode.writePNG(path, image)
        return how

    def reportTexture(self, drawcallId, rid, desc, size, position, count, how):
        self.texture_report["saved"] += 1
        key = (position, count, how)
        if key in self.texture_report["choices"]:
            return
        self.texture_report["choices"].add(key)
        what = "unknown format"
        if desc is not None:
            what = f"{desc.width}x{desc.height} {desc.format.Name()}"
        print(f"Texture choice for drawcall {drawcallId}: bound slot {position} of {count}, "
              f"{what}, {size} bytes, written by {how}")

def main(controller):
    scraper = CaptureScraper(controller, CAPTURE_FILE)
    scraper.run()

if __name__ == "__main__":
    if 'pyrenderdoc' in globals():
        pyrenderdoc.Replay().BlockInvoke(main)
    else:
        print("Loading capture from {}...".format(CAPTURE_FILE))
        with CaptureWrapper(CAPTURE_FILE) as controller:
            if controller is None:
                print("Error: Could not open the capture file.")
                sys.exit(1)
            main(controller)
