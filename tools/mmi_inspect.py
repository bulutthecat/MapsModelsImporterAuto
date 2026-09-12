#!/usr/bin/env python3
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

"""Report what is actually inside a capture.

When an import fails with "could not find any relevant draw call", the useful
question is what the capture *does* contain. This groups the draw calls by the
vertex shader that issued them and prints, for the busiest shaders, the
constants and vertex attributes they declare -- which is exactly what the
importer matches on.

Run through `tools/mmi inspect capture.rdc`, which arranges for the renderdoc
module to be importable.
"""

import argparse
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "blender", "MapsModelsImporter"),
)

try:
    import renderdoc as rd
except ImportError as err:  # pragma: no cover - depends on the environment
    sys.stderr.write(f"Could not import the renderdoc python module: {err}\n")
    sys.exit(20)

# google_maps_rd.py is a script first and a module second: it reads its
# arguments at import time. Give it a harmless set before importing it.
_REAL_ARGV = sys.argv
sys.argv = [_REAL_ARGV[0], "", "", "-1"]

import rdcompat
from rdcompat import Drawcall
from rdutils import CaptureWrapper
from google_maps_rd import CaptureScraper
sys.argv = _REAL_ARGV


def collectActions(controller, roots, accumulator=None):
    if accumulator is None:
        accumulator = []
    sdfile = controller.GetStructuredFile()
    for action in roots:
        accumulator.append(Drawcall(action, action.GetName(sdfile).split('::', 1)[-1]))
        collectActions(controller, action.children, accumulator)
    return accumulator


def describe(controller, draw):
    controller.SetFrameEvent(draw.eventId, False)
    state = controller.GetPipelineState()
    reflection = state.GetShaderReflection(rd.ShaderStage.Vertex)

    constants = []
    if reflection is not None:
        for block in reflection.constantBlocks:
            for var in block.variables:
                constants.append((block.name, var.name, var.type.rows, var.type.columns))

    attributes = [(a.name, a.format.compCount) for a in state.GetVertexInputs()]
    textures = len(rdcompat.getReadOnlyResources(state, rd.ShaderStage.Fragment))
    return str(state.GetShader(rd.ShaderStage.Vertex)), constants, attributes, textures


def shaderSource(reflection):
    if reflection is None:
        return ""
    return "\n".join(f.contents for f in reflection.debugInfo.files)


def sampleValues(scraper, draws, names, samples=3):
    """Values of the named constants over a few draw calls, to show which of
    them change from one tile to the next."""
    step = max(1, len(draws) // samples)
    rows = []
    for draw in draws[::step][:samples]:
        constants = scraper.getVertexShaderConstants(draw)
        merged = {}
        for block in constants.values():
            if isinstance(block, dict):
                merged.update(block)
        rows.append({n: merged.get(n) for n in names})
    return rows


def fmt(values, limit=16):
    if values is None:
        return "?"
    vals = list(values)[:limit]
    return "[" + ", ".join(f"{v:.6g}" for v in vals) + ("]" if len(values) <= limit else ", ...]")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", help="The .rdc file to inspect")
    parser.add_argument("--top", type=int, default=5,
                        help="How many of the busiest shaders to detail")
    parser.add_argument("--all-constants", action="store_true",
                        help="Print every constant, not just the matrices and vec4s")
    parser.add_argument("--source", action="store_true",
                        help="Print the vertex shader source of each detailed shader")
    parser.add_argument("--values", action="store_true",
                        help="Print the matrix and vec4 values for a few draw calls per shader")
    args = parser.parse_args(argv)

    with CaptureWrapper(args.capture) as controller:
        if controller is None:
            return 1

        actions = collectActions(controller, controller.GetRootActions())
        draws = [a for a in actions if rdcompat.isDrawcall(a)]
        indexed = [a for a in draws if rdcompat.isIndexedDrawcall(a)]

        print(f"Capture: {args.capture}")
        print(f"  {len(actions)} events, {len(draws)} draw calls, {len(indexed)} indexed")
        if draws:
            names = {}
            for a in draws:
                names[a.name.split('(')[0]] = names.get(a.name.split('(')[0], 0) + 1
            print("  draw call names: "
                  + ", ".join(f"{n} x{c}" for n, c in sorted(names.items(),
                                                             key=lambda kv: -kv[1])[:6]))
        print()

        if not indexed:
            print("No indexed draw call at all. The map geometry is always indexed,")
            print("so this capture does not contain it: either nothing 3D was on")
            print("screen, or the page is being drawn by a software fallback that")
            print("RenderDoc cannot see (check chrome://gpu).")
            return 2

        print(f"Grouping {len(indexed)} indexed draw calls by vertex shader...")
        scraper = CaptureScraper(controller)
        groups = {}
        for draw in indexed:
            shader, constants, attributes, textures = describe(controller, draw)
            entry = groups.setdefault(
                shader,
                {"count": 0, "constants": constants, "attributes": attributes,
                 "textures": textures, "draws": [], "reflection": None},
            )
            entry["count"] += 1
            entry["draws"].append(draw)

        ordered = sorted(groups.items(), key=lambda kv: -kv[1]["count"])
        print(f"{len(ordered)} distinct vertex shaders.\n")

        for shader, entry in ordered[:args.top]:
            constants = entry["constants"]
            matrices = [(b, n) for b, n, r, c in constants if r == 4 and c == 4]
            vectors = [(b, n) for b, n, r, c in constants if r == 1 and c == 4]
            angle = any(n.startswith("webgl_") or n.startswith("_webgl_")
                        for _, n, _, _ in constants)

            print(f"shader {shader}: {entry['count']} draw calls")
            print("  vertex attributes: "
                  + (", ".join(f"{n}({c})" for n, c in entry["attributes"]) or "none"))
            print(f"  textures bound to the fragment shader: {entry['textures']}")
            print(f"  4x4 matrices: {[n for _, n in matrices] or 'none'}")
            print(f"  vec4s: {[n for _, n in vectors] or 'none'}")
            if angle:
                print("  names are ANGLE hashes (Chrome's WebGL), so they carry no meaning")
            if args.all_constants:
                print("  all constants:")
                for block, name, rows, columns in constants:
                    print(f"    {block}.{name}: {rows}x{columns}")
            looks_right = bool(matrices) and bool(vectors) and len(entry["attributes"]) >= 2
            print(f"  looks like textured 3D tiles: {'yes' if looks_right else 'no'}")

            if looks_right:
                varying = scraper.findVaryingConstants(entry["draws"])
                print("  constants that change between draw calls: "
                      + (", ".join(sorted(varying)) or "none"))
                print("  matrices changing per draw (the tile placement lives here): "
                      + (", ".join(n for _, n in matrices if n in varying) or "NONE"))

            if args.values and looks_right:
                names = [n for _, n in matrices] + [n for _, n in vectors]
                for i, row in enumerate(sampleValues(scraper, entry["draws"], names)):
                    print(f"  sample draw #{i}:")
                    for name in names:
                        print(f"    {name} = {fmt(row[name])}")

            if args.source:
                controller.SetFrameEvent(entry["draws"][0].eventId, False)
                state = controller.GetPipelineState()
                source = shaderSource(state.GetShaderReflection(rd.ShaderStage.Vertex))
                print("  vertex shader source:")
                print("    " + "\n    ".join(source.splitlines()) if source else "    (not available)")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
