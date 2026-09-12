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

import rdcompat
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

def numpySave(array, file):
    np.array([array.ndim], dtype=np.int32).tofile(file)
    np.array(array.shape, dtype=np.int32).tofile(file)
    dt = array.dtype.descr[0][1][1:3].encode('ascii')
    file.write(dt)
    array.tofile(file)

class CaptureScraper():
    def __init__(self, controller):
        self.controller = controller
        self._uniform_names_cache = {}
        self._signature_cache = {}
        # Names the structural strategy worked out, handed to the importer so
        # that it knows which constant is the matrix and which the UV
        # transform when it cannot recognise them by name.
        self.uniform_hints = None
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
            self.extractTexture(drawcallId, state)
            profiling_counters['extractTexture'].add_sample(subtimer)

            profiling_counters['processDrawEvent'].add_sample(timer)

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

    def extractTexture(self, drawcallId, state):
        """Save the texture in a png file (A bit dirty)"""
        rid = rdcompat.getLastReadOnlyResource(state, rd.ShaderStage.Fragment)
        if rid is None or rid == rd.ResourceId.Null():
            print(f"Warning: No texture found for drawcall {drawcallId}")
            return

        texsave = rd.TextureSave()
        texsave.resourceId = rid
        texsave.mip = 0
        texsave.slice.sliceIndex = 0
        texsave.alpha = rd.AlphaMapping.Preserve
        texsave.destType = rd.FileType.PNG
        timer = Timer()
        self.controller.SaveTexture(texsave, "{}{:05d}-texture.png".format(FILEPREFIX, drawcallId))
        profiling_counters["SaveTexture"].add_sample(timer)

def main(controller):
    scraper = CaptureScraper(controller)
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
