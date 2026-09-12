# This file is part of SnapPortal
#
# Copyright (c) 2020-2026 -- Télécom Paris (Élie Michel <elie.michel@telecom-paris.fr>)
#
# The MIT license:
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the “Software”), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# The Software is provided “as is”, without warranty of any kind, express or
# implied, including but not limited to the warranties of merchantability,
# fitness for a particular purpose and non-infringement. In no event shall the
# authors or copyright holders be liable for any claim, damages or other
# liability, whether in an action of contract, tort or otherwise, arising
# from, out of or in connection with the software or the use or other dealings
# in the Software.

# This file is part of MapsModelsImporter, a set of addons to import 3D models
# from Maps services

# This experimental version tries a new way of extracting draw calls: rather
# than locating the batch of draw calls that sits between two known API calls,
# it keeps every draw call whose vertex shader declares the full set of
# uniforms the Google Maps shader uses.
#
# Everything else (reading the capture, writing the intermediate files) is
# shared with google_maps_rd.py.

import sys

import rdcompat
from google_maps_rd import CAPTURE_FILE, CaptureScraper
from rdutils import CaptureWrapper

# The complete set of constants the Google Maps vertex shader declares. Asking
# for all of them is what makes this strategy more selective than the one in
# google_maps_rd.py.
GOOGLE_MAPS_UNIFORMS = ["_w", "_s", "_u", "_t", "_x", "_A", "_B", "_C", "_D", "_E"]

class ExperimentalCaptureScraper(CaptureScraper):
    def extractRelevantCalls(self, drawcalls, _strategy=None):
        """List the drawcalls related to drawing the 3D meshes thanks to an ad
        hoc heuristic. It may be different in RenderDoc UI and in the Python
        module, for some reason."""

        def isDrawCallValid(dc):
            """Return true iff this is a draw call that draws 3D maps data"""
            if not rdcompat.isIndexedDrawcall(dc):
                return False
            uniforms = self.getVertexUniformNames(dc)
            return all(u in uniforms for u in GOOGLE_MAPS_UNIFORMS)

        relevant_drawcalls = [dc for dc in drawcalls if isDrawCallValid(dc)]
        capture_type = "Google Maps"

        print(f"Found {len(relevant_drawcalls)} relevant draw calls.")
        return relevant_drawcalls, capture_type

def main(controller):
    scraper = ExperimentalCaptureScraper(controller)
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
