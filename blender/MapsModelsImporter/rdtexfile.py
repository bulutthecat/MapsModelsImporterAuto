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

"""Texture contents read straight out of the capture file.

A RenderDoc capture holds every upload the application made to a texture:
the `glCompressedTexImage2D` / `glTexSubImage2D` calls recorded when the
texture was filled, and an "Initial Contents" snapshot of what the GPU held
when the frame started. Replaying the capture rebuilds the texture from
those, and GetTextureData() then reads it back from the GPU. When that
read-back comes back all zero -- which happens with Chrome's tile textures
on some drivers, while the same capture opened in qrenderdoc shows the
uploads are all there -- this module skips the GPU altogether and takes the
bytes from the file.

Only OpenGL captures are understood, which is what a browser gives us on
Linux. The result is the level-0 image in the same layout GetTextureData()
would return: compressed blocks for BC formats, tightly packed pixels for
8-bit ones, bottom row first as OpenGL stores them.
"""

import numpy as np

import renderdoc as rd


# GL enums we need to recognise, so as not to depend on names the module may
# or may not expose.
GL_UNSIGNED_BYTE = 0x1401
GL_RGB = 0x1907
GL_RGBA = 0x1908
GL_BGRA = 0x80E1
GL_RED = 0x1903
GL_RG = 0x8227

CHANNELS = {GL_RGBA: 4, GL_BGRA: 4, GL_RGB: 3, GL_RG: 2, GL_RED: 1}


class Upload:
    """One recorded write into a texture's level 0."""

    __slots__ = ("order", "eventId", "x", "y", "width", "height", "full",
                 "buffer", "compressed", "channels", "chunk")

    def __init__(self, order, eventId, x, y, width, height, full, buffer,
                 compressed, channels, chunk):
        self.order = order
        self.eventId = eventId
        self.x, self.y = x, y
        self.width, self.height = width, height
        self.full = full
        self.buffer = buffer
        self.compressed = compressed
        self.channels = channels
        self.chunk = chunk


def blockBytes(fmt):
    """Bytes per 4x4 block for the compressed formats we decode, else None."""
    kind = getattr(fmt, "type", None)
    if kind == rd.ResourceFormatType.BC1:
        return 8
    if kind in (rd.ResourceFormatType.BC2, rd.ResourceFormatType.BC3):
        return 16
    return None


def isAllZero(data):
    if not data:
        return True
    return not np.frombuffer(bytes(data), dtype=np.uint8).any()


class CaptureFileTextures:
    """Index of the texture uploads stored in a capture file.

    Built lazily and only once: parsing the file's structured data again
    costs a couple of seconds and holds every upload in memory, which is
    fine for the hundred-megabyte captures a map view produces.
    """

    def __init__(self, filename):
        self.filename = filename
        self.loaded = False
        self.error = None
        self.cap = None
        self.sdfile = None
        self.uploads = {}       # ResourceId -> [Upload], in recording order
        self.initial = {}       # ResourceId -> buffer index of the initial contents
        self.skipped = 0        # uploads we could not use (PBO-sourced, 3D...)

    # -- loading ------------------------------------------------------------

    def load(self, controller=None):
        """Parse the file. Returns True when there is something to read."""
        if self.loaded:
            return self.error is None
        self.loaded = True
        try:
            self._load(controller)
        except Exception as err:  # noqa: BLE001 - anything here is a reason to give up
            self.error = f"{type(err).__name__}: {err}"
            self.close()
            return False
        return True

    def _load(self, controller):
        cap = rd.OpenCaptureFile()
        status = cap.OpenFile(self.filename, '', None)
        if not status.OK():
            cap.Shutdown()
            raise RuntimeError(f"could not open {self.filename}: {status.Message()}")
        # Unlike the replay's own structured file, this one carries the byte
        # buffers of every chunk, and needs no GPU to be produced. It belongs
        # to the CaptureFile, which therefore has to outlive our reads.
        self.cap = cap
        self.sdfile = cap.GetStructuredData()

        # Chunks issued during the frame map to events; everything else was
        # recorded before the frame and applies in file order.
        event_of_chunk = {}
        if controller is not None:
            self._collectEvents(controller.GetRootActions(), event_of_chunk)

        for index, chunk in enumerate(self.sdfile.chunks):
            texture = chunk.FindChild("texture")
            if texture is None:
                texture = chunk.FindChild("id")
            if texture is None or texture.type.basetype != rd.SDBasic.Resource:
                continue
            rid = texture.AsResourceId()

            if chunk.name.endswith("Initial Contents"):
                contents = chunk.FindChild("SubresourceContents")
                if contents is not None and contents.type.basetype == rd.SDBasic.Buffer:
                    self.initial[rid] = int(contents.data.basic.u)
                continue

            upload = self._parseUpload(chunk, index, event_of_chunk.get(index))
            if upload is not None:
                self.uploads.setdefault(rid, []).append(upload)

    def _collectEvents(self, actions, event_of_chunk):
        for action in actions:
            for event in action.events:
                event_of_chunk[event.chunkIndex] = event.eventId
            self._collectEvents(action.children, event_of_chunk)

    def _parseUpload(self, chunk, index, eventId):
        pixels = chunk.FindChild("pixels")
        level = chunk.FindChild("level")
        width = chunk.FindChild("width")
        height = chunk.FindChild("height")
        if pixels is None or level is None or width is None or height is None:
            return None
        if chunk.FindChild("depth") is not None or chunk.FindChild("zoffset") is not None:
            return None    # 3D and array uploads: not a tile
        if level.AsInt() != 0:
            return None
        if pixels.type.basetype != rd.SDBasic.Buffer:
            # Sourced from a pixel unpack buffer: the bytes are elsewhere.
            self.skipped += 1
            return None

        compressed = chunk.FindChild("imageSize") is not None
        channels = None
        if not compressed:
            fmt = chunk.FindChild("format")
            kind = chunk.FindChild("type")
            if fmt is None or kind is None or kind.AsInt() != GL_UNSIGNED_BYTE:
                self.skipped += 1
                return None
            channels = CHANNELS.get(fmt.AsInt())
            if channels is None:
                self.skipped += 1
                return None

        xoffset = chunk.FindChild("xoffset")
        yoffset = chunk.FindChild("yoffset")
        x = xoffset.AsInt() if xoffset is not None else 0
        y = yoffset.AsInt() if yoffset is not None else 0
        full = xoffset is None
        return Upload(index, eventId, x, y, width.AsInt(), height.AsInt(), full,
                      int(pixels.data.basic.u), compressed, channels, chunk.name)

    def close(self):
        """Release the file and the uploads it holds in memory."""
        self.sdfile = None
        self.uploads = {}
        self.initial = {}
        if self.cap is not None:
            self.cap.Shutdown()
            self.cap = None

    # -- reading ------------------------------------------------------------

    def bufferBytes(self, index):
        return bytes(self.sdfile.buffers[index])

    def describe(self, rid):
        """A one-line account of what the file holds for a texture."""
        parts = []
        if rid in self.initial:
            data = self.bufferBytes(self.initial[rid])
            parts.append(f"initial contents {len(data)} bytes"
                         + (" (all zero)" if isAllZero(data) else ""))
        uploads = self.uploads.get(rid, [])
        if uploads:
            names = {}
            for up in uploads:
                names[up.chunk] = names.get(up.chunk, 0) + 1
            parts.append("uploads: " + ", ".join(f"{n} x{c}" for n, c in names.items()))
        return "; ".join(parts) if parts else "nothing"

    def levelZero(self, rid, desc, beforeEvent=None):
        """Level 0 of the texture as the draw call at `beforeEvent` saw it,
        or None when the file holds no data for it at all."""
        if self.sdfile is None:
            return None
        block = blockBytes(desc.format)
        if block is None:
            fmt = desc.format
            if getattr(fmt, "type", None) != rd.ResourceFormatType.Regular or fmt.compByteWidth != 1:
                return None
            pixel = fmt.compCount
        width, height = desc.width, desc.height

        def canvas():
            if block is not None:
                return np.zeros(((height + 3) // 4, (width + 3) // 4, block), np.uint8)
            return np.zeros((height, width, pixel), np.uint8)

        # What the texture held when the frame started: the snapshot if
        # RenderDoc managed to take one, else the recorded uploads replayed
        # in order.
        image = None
        found = False
        if rid in self.initial:
            data = self.bufferBytes(self.initial[rid])
            expected = canvas().nbytes
            if len(data) >= expected and not isAllZero(data[:expected]):
                image = np.frombuffer(data[:expected], np.uint8).reshape(canvas().shape).copy()
                found = True

        uploads = self.uploads.get(rid, [])
        before = [up for up in uploads if up.eventId is None]
        during = sorted((up for up in uploads
                         if up.eventId is not None
                         and (beforeEvent is None or up.eventId < beforeEvent)),
                        key=lambda up: up.eventId)

        if image is None:
            image = canvas()
            for up in before:
                found |= self._apply(image, up, block, pixel if block is None else None)
        for up in during:
            found |= self._apply(image, up, block, pixel if block is None else None)

        return image.tobytes() if found else None

    def _apply(self, image, up, block, pixel):
        """Paste one upload into the image. Returns False when it could not
        be used (wrong format for this texture, odd size)."""
        if up.compressed != (block is not None):
            return False
        data = self.bufferBytes(up.buffer)
        if block is not None:
            bw, bh = (up.width + 3) // 4, (up.height + 3) // 4
            if up.x % 4 or up.y % 4 or len(data) < bw * bh * block:
                return False
            bx, by = up.x // 4, up.y // 4
            region = image[by:by + bh, bx:bx + bw]
            if region.shape[:2] != (bh, bw):
                return False
            region[...] = np.frombuffer(data[:bw * bh * block], np.uint8).reshape(bh, bw, block)
            return not isAllZero(data[:bw * bh * block])

        if up.channels != pixel:
            return False
        rowbytes = up.width * pixel
        # GL_UNPACK_ALIGNMENT defaults to 4: rows are padded to a multiple of 4.
        stride = (rowbytes + 3) // 4 * 4
        if len(data) < stride * (up.height - 1) + rowbytes:
            return False
        rows = np.frombuffer(data[:stride * up.height] if len(data) >= stride * up.height
                             else data + bytes(stride * up.height - len(data)), np.uint8)
        rows = rows.reshape(up.height, stride)[:, :rowbytes].reshape(up.height, up.width, pixel)
        region = image[up.y:up.y + up.height, up.x:up.x + up.width]
        if region.shape[:2] != (up.height, up.width):
            return False
        region[...] = rows
        return bool(rows.any())
