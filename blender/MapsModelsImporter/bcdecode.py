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

"""Turn raw texture bytes into a PNG, without RenderDoc's help.

Google Maps ships its tile textures as BC1 (DXT1). RenderDoc's SaveTexture()
on an OpenGL replay reports success for those and writes an image of the
right size that is entirely black -- while GetTextureData() returns the
compressed blocks intact. So the blocks are decoded here, on the CPU, which
also makes the result independent of whatever the replay driver does.

Only what the importer needs: BC1/BC2/BC3 (the colour half, alpha is not
used) and plain 8-bit RGBA/BGRA. numpy only, no image library.
"""

import struct
import zlib

import numpy as np


# -----------------------------------------------------------------------------
# Block compression

def _rgb565(c):
    """(N,) uint16 -> (N, 3) uint8, expanding each channel the way GPUs do."""
    r = ((c >> 11) & 0x1F).astype(np.uint16)
    g = ((c >> 5) & 0x3F).astype(np.uint16)
    b = (c & 0x1F).astype(np.uint16)
    return np.stack(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)), axis=-1).astype(np.uint8)


def decodeBC1Blocks(blocks, four_colour_only=False):
    """Decode (N, 8) BC1 colour blocks into (N, 16, 3) RGB pixels.

    four_colour_only is for the colour half of BC2/BC3, which is always in
    the 4-colour mode whatever the order of its two endpoints.
    """
    blocks = np.asarray(blocks, dtype=np.uint8).reshape(-1, 8)
    c0 = blocks[:, 0].astype(np.uint16) | (blocks[:, 1].astype(np.uint16) << 8)
    c1 = blocks[:, 2].astype(np.uint16) | (blocks[:, 3].astype(np.uint16) << 8)
    rgb0 = _rgb565(c0).astype(np.int32)
    rgb1 = _rgb565(c1).astype(np.int32)

    four = (c0 > c1) | four_colour_only
    four3 = four[:, None]
    rgb2 = np.where(four3, (2 * rgb0 + rgb1) // 3, (rgb0 + rgb1) // 2)
    rgb3 = np.where(four3, (rgb0 + 2 * rgb1) // 3, 0)

    palette = np.stack((rgb0, rgb1, rgb2, rgb3), axis=1).astype(np.uint8)  # (N, 4, 3)

    bits = (blocks[:, 4].astype(np.uint32)
            | (blocks[:, 5].astype(np.uint32) << 8)
            | (blocks[:, 6].astype(np.uint32) << 16)
            | (blocks[:, 7].astype(np.uint32) << 24))
    shifts = np.arange(16, dtype=np.uint32) * 2
    indices = ((bits[:, None] >> shifts[None, :]) & 3).astype(np.intp)  # (N, 16)

    return np.take_along_axis(palette, indices[:, :, None], axis=1)  # (N, 16, 3)


def decodeBC(data, width, height, block_bytes, colour_offset):
    """Decode a whole BC1/BC2/BC3 image to (height, width, 3) uint8."""
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    expected = bw * bh * block_bytes
    data = np.frombuffer(bytes(data), dtype=np.uint8)
    if len(data) < expected:
        raise ValueError(f"expected {expected} bytes of {block_bytes}-byte blocks, got {len(data)}")
    blocks = data[:expected].reshape(bh * bw, block_bytes)[:, colour_offset:colour_offset + 8]
    pixels = decodeBC1Blocks(blocks, four_colour_only=block_bytes == 16)
    image = pixels.reshape(bh, bw, 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 3)
    return image[:height, :width]


def decodeBC1(data, width, height):
    return decodeBC(data, width, height, 8, 0)


def decodeBC2(data, width, height):
    return decodeBC(data, width, height, 16, 8)


decodeBC3 = decodeBC2


# -----------------------------------------------------------------------------
# Plain formats

def decodeRGBA8(data, width, height, comp_count, bgra=False):
    data = np.frombuffer(bytes(data), dtype=np.uint8)
    expected = width * height * comp_count
    if len(data) < expected:
        raise ValueError(f"expected {expected} bytes, got {len(data)}")
    image = data[:expected].reshape(height, width, comp_count)
    if comp_count == 1:
        image = np.repeat(image, 3, axis=2)
    elif comp_count == 2:
        image = np.concatenate((image, np.zeros((height, width, 1), np.uint8)), axis=2)
    else:
        image = image[:, :, :3]
    if bgra:
        image = image[:, :, ::-1]
    return image


# -----------------------------------------------------------------------------
# PNG output

def writePNG(path, rgb):
    """Write an (H, W, 3) uint8 array as an opaque RGBA PNG."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    height, width = rgb.shape[:2]
    rgba = np.concatenate((rgb, np.full((height, width, 1), 255, np.uint8)), axis=2)
    # One filter byte (0 = none) in front of every row.
    rows = np.concatenate((np.zeros((height, 1), np.uint8), rgba.reshape(height, -1)), axis=1)

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    with open(path, "wb") as file:
        file.write(b"\x89PNG\r\n\x1a\n")
        file.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)))
        file.write(chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)))
        file.write(chunk(b"IEND", b""))


# -----------------------------------------------------------------------------
# Reference implementation, used by the self-test only

def _referenceBC1Block(block, four_colour_only=False):
    c0 = block[0] | (block[1] << 8)
    c1 = block[2] | (block[3] << 8)

    def expand(c):
        r, g, b = (c >> 11) & 0x1F, (c >> 5) & 0x3F, c & 0x1F
        return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))

    p0, p1 = expand(c0), expand(c1)
    if c0 > c1 or four_colour_only:
        p2 = tuple((2 * a + b) // 3 for a, b in zip(p0, p1))
        p3 = tuple((a + 2 * b) // 3 for a, b in zip(p0, p1))
    else:
        p2 = tuple((a + b) // 2 for a, b in zip(p0, p1))
        p3 = (0, 0, 0)
    palette = (p0, p1, p2, p3)
    bits = block[4] | (block[5] << 8) | (block[6] << 16) | (block[7] << 24)
    return [palette[(bits >> (2 * i)) & 3] for i in range(16)]


def selfTest():
    rng = np.random.default_rng(1)
    blocks = rng.integers(0, 256, size=(500, 8), dtype=np.uint8)
    fast = decodeBC1Blocks(blocks)
    for i, block in enumerate(blocks):
        reference = _referenceBC1Block([int(b) for b in block])
        assert fast[i].tolist() == [list(p) for p in reference], f"block {i} differs"
    # The layout: a 8x4 image is two blocks side by side.
    two = np.zeros((2, 8), np.uint8)
    two[0, 0:2] = (0x00, 0xF8)  # red, index 0 everywhere
    two[1, 0:2] = (0x1F, 0x00)  # blue
    image = decodeBC1(two.tobytes(), 8, 4)
    assert image.shape == (4, 8, 3)
    assert image[0, 0].tolist() == [255, 0, 0] and image[3, 7].tolist() == [0, 0, 255]
    return True


if __name__ == "__main__":
    print("bcdecode self-test:", "ok" if selfTest() else "failed")
