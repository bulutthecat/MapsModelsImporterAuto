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

import renderdoc as rd

import rdcompat

class CaptureWrapper():
    """Open a capture file for replay, as a context manager.

    Yields a ReplayController, or None if the capture could not be opened (in
    which case the reason has been printed out).
    """

    def __init__(self, filename):
        self.filename = filename
        self.err = False
        self.initialised = False

    def __enter__(self):
        # Mandatory when driving RenderDoc from a plain python interpreter
        # rather than from its own UI.
        rdcompat.initialiseReplay()
        self.initialised = True

        self.cap = rd.OpenCaptureFile()
        status = self.cap.OpenFile(self.filename, '', None)

        if not status.OK():
            print("Couldn't open file: " + str(status.Message()))
            self.err = True
            return None

        if not self.cap.LocalReplaySupport():
            print("Capture cannot be replayed")
            self.err = True
            return None
        
        status, self.controller = self.cap.OpenCapture(rd.ReplayOptions(), None)

        if not status.OK():
            print("Couldn't initialise replay: " + str(status.Message()))
            print("This is often due to a mismatch between the version of "
                  "RenderDoc that took the capture and the one replaying it.")
            self.cap.Shutdown()
            self.err = True
            return None

        # Once the replay is created, the CaptureFile can be shut down, there
        # is no dependency on it by the ReplayController.
        self.cap.Shutdown()

        return self.controller
        
    def __exit__(self, type, value, traceback):
        if not self.err:
            self.controller.Shutdown()
        if self.initialised:
            rdcompat.shutdownReplay()
