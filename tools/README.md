Maps Models Importer on Linux
=============================

`mmi` is a single script that brings up the whole Google Maps / Google Earth
capture pipeline on Linux, without the Windows-only steps of the classic
workflow.

```
tools/mmi setup     # once: install dependencies, build RenderDoc's python module
tools/mmi up        # mount the add-on, start the browser under RenderDoc
tools/mmi capture   # from another terminal: take a capture (auto-imported)
```

Why Linux needed a different approach
-------------------------------------

The documented workflow asks you to start Chrome with `--gpu-startup-dialog`
and then *inject* RenderDoc into the paused GPU process. RenderDoc's injection
feature only exists on Windows, which is why the README says Linux users can
only import captures made elsewhere.

Launching a process from RenderDoc does work on Linux, and RenderDoc can follow
a process into the children it spawns. Chrome's GPU process is such a child, so
launching the browser with *capture child processes* enabled achieves the same
result as injection — no startup dialog, no manual process picking, no timing
game. `mmi` drives that through RenderDoc's python API, and also triggers the
captures for you over a target-control connection, so `Print Screen` timing is
no longer something you have to get right by hand.

The second Linux obstacle is the `renderdoc` python module. RenderDoc's
official Linux tarball does not ship one — it has to be built from source
against one specific python version. `mmi setup` does that build (without the
Qt UI, so it is much lighter than a full RenderDoc build), and the add-on has
been changed so that it can use *any* interpreter that can import the module
rather than insisting on Blender's own python.

Requirements
------------

* Ubuntu 22.04 or newer (any Debian-ish distribution should work; other
  distributions need their own equivalent of the package list, see
  `APT_PACKAGES` in the script).
* Blender 4.2 or newer.
* A GPU with working hardware acceleration, and an X11 session (on Wayland the
  browser is started through XWayland, which is the combination RenderDoc
  supports).
* A browser -- and `mmi` brings its own. `up` uses the self-contained Chrome
  that `tools/mmi install-chromium` downloads (Google's "Chrome for Testing"
  build, no root needed), fetching it first if it is missing. It never falls
  back to the distribution's browser on its own: Ubuntu's Chromium build has
  been seen to crash its GPU process under RenderDoc, after which Chromium
  switches to software rendering and captures contain no 3D. To use another
  browser anyway, set `MMI_BROWSER` or pass `--browser` (not a snap or
  flatpak: their confinement prevents the hooking).

Commands
--------

| Command | What it does |
| --- | --- |
| `setup` | Installs build dependencies and builds RenderDoc + its python module into `~/.local/share/maps-models-importer` |
| `doctor` | Checks every piece and tells you what is missing |
| `mount` | Symlinks the add-on into Blender's extensions directory, enables it, and points it at the RenderDoc module |
| `unmount` | Undoes `mount` |
| `up` | `mount`, then launch the browser under RenderDoc with auto-import enabled |
| `launch` | Same, without mounting or auto-importing |
| `capture` | Triggers a capture in the running session |
| `status` | Reports what the session is hooked into |
| `stop` | Ends the session |
| `import FILE.rdc` | Imports one capture into a `.blend` with headless Blender |
| `watch` | Imports every capture that appears in the capture directory |
| `inspect FILE.rdc` | Reports what a capture contains, when an import finds nothing in it |
| `install-chromium` | Downloads a self-contained Chrome into `MMI_HOME`, no root needed |
| `install-chrome` | Installs Google Chrome from Google's apt repository |
| `env` | Prints the resolved paths |
| `clean [captures\|output\|build\|all]` | Housekeeping |

Walkthrough
-----------

### 1. Set up (once, 10-30 minutes)

```
tools/mmi setup
```

This installs the build dependencies, clones RenderDoc at a pinned tag
(v1.46 by default), and builds it with `-DENABLE_QRENDERDOC=OFF`, so only the
replay library, `renderdoccmd` and the python module get built. The module is
built against your distribution's `python3`, and `python3-numpy` is installed
alongside it, since those two modules are all the extraction step needs.

Everything lands in `~/.local/share/maps-models-importer`; nothing is installed
system-wide, and `tools/mmi clean all` removes it again.

If your machine cannot reach github during the build (RenderDoc downloads its
own SWIG fork), download
`https://github.com/baldurk/swig/archive/renderdoc-modified-7.zip` by hand and
pass `--swig-package /path/to/that.zip`.

### 2. Bring everything up

```
tools/mmi up
```

Useful variations: `--replace` stops a browser left over from an earlier
session, `--earth` opens Google Earth instead, and `--in-process-gpu` is the
fallback for machines where the GPU process never gets hooked.

`up` mounts the add-on into Blender (symlink + enable + configure, saved into
your Blender preferences), then starts the browser with RenderDoc attached and
opens Google Maps. Use `--earth` for Google Earth, or `--url` for anywhere
else. The terminal stays busy: that is the session, and it logs every process
RenderDoc hooks and every capture that is taken.

Go to the 3D view you want, in satellite mode with 3D enabled.

### 3. Take a capture

From another terminal:

```
tools/mmi capture --delay 5
```

**Google Maps only streams the 3D geometry while the view is moving**, so start
dragging the view during the countdown and keep dragging until the capture is
reported. Google Earth does not need this.

You can also press `F12` (or `Print Screen`) in the browser window — RenderDoc's
usual capture keys work, and the session picks those captures up too.

With `up` (or `launch --auto-import`), each capture is imported into Blender
headlessly as soon as it is taken, producing
`~/.local/share/maps-models-importer/output/<capture>.blend`. Add `--glb` to
also get a glTF next to it.

If you would rather import by hand, `tools/mmi import path/to/capture.rdc`
does one file, and the add-on's usual `File > Import > Google Maps Capture`
menu entry works in the GUI as well, since `mount` configured it.

Choosing the graphics API
-------------------------

`--api gl` (the default) forces ANGLE onto desktop OpenGL and tells RenderDoc
to leave EGL alone (`RENDERDOC_HOOK_EGL=0`), which is the Linux counterpart of
the `RENDERDOC_HOOK_EGL=0` line in the Windows instructions.

If `tools/mmi status` reports no graphics API on any hooked process, the
browser is talking to the driver through a path RenderDoc is not hooking. Try:

* `--api gl-egl`, which lets RenderDoc hook EGL instead of GLX;
* `--api gles`, which runs ANGLE on the driver's OpenGL ES (through EGL)
  rather than desktop OpenGL. RenderDoc captures ES as well, and on ES it
  keeps its own copy of every compressed texture upload instead of reading
  the texture back from the driver, which matters when a capture comes out
  with empty tile textures (see the troubleshooting entry below);
* `--api vulkan`, which runs ANGLE on Vulkan and captures that (the RenderDoc
  Vulkan layer is registered for your user by `setup`).

Troubleshooting
---------------

**`doctor` says no browser is usable.** You most likely have the snap Chromium.
Run `tools/mmi install-chromium`, which downloads one that does not depend on
your distribution at all.

**The browser dies immediately with `zygote_host_impl_linux.cc ... Check
failed: No such process`.** Chromium normally forks its child processes from a
"zygote" and then validates that process' PID through a handshake across a PID
namespace. RenderDoc's child-process hooking breaks that handshake and the
browser aborts. `mmi` therefore passes `--no-zygote` (and `--no-sandbox`, which
Chromium requires alongside it), so children are forked and exec'd directly.
If you are launching the browser by hand, you need both of those flags.

**`chrome://gpu` says "GPU process was unable to boot: GPU access is disabled
due to frequent crashes", everything is "Software only", and the session log
shows "the GPU process crashed".** The GPU process died three times and
Chromium gave up on the GPU for the rest of the session, so WebGL now runs in
SwiftShader, which RenderDoc cannot see. Two things were done about this:
`mmi` passes `--disable-gpu-process-crash-limit`, `--ignore-gpu-blocklist` and
`--disable-software-rasterizer`, so the GPU is used or the page fails visibly
rather than quietly degrading; and it insists on its own Chrome build, the
only one seen to survive with RenderDoc in its GPU process. If the crashes
persist with that build, the session log counts them; that is the information
to report.

**A browser from a previous session is still running.** `mmi` tells you the
pid and refuses to start, because a second instance on the same profile would
just hand its command line to the first one and exit. Close it, or use
`tools/mmi up --replace`.

**The import says "could not find any relevant draw call", but the capture is
large and `inspect` shows thousands of indexed draw calls.** The geometry is
there; it is the uniform *names* that no longer match. Chrome renders WebGL
through ANGLE, which rewrites every uniform to `webgl_<16 hex digits>`, hashed
from the original name. The two hashes hard-coded in `extractUniforms()` were
taken from one version of the Google Maps shaders years ago, and every one of
them changes whenever Google edits a shader.

The importer therefore falls back on the shape of the draw calls: the tiles are
drawn by whichever shader accounts for most of the indexed draw calls while
holding a 4x4 matrix and a vec4 and taking a position and a UV attribute. It
then picks between the candidate vec4s by trying each and keeping the one that
maps the mesh's UVs into [0, 1]. The log says which constants it settled on:

```
Using 'webgl_a1b2c3d4e5f60718' as the model matrix and 'webgl_0f1e2d3c4b5a6978' (direct) as the UV transform
```

If the geometry imports but the textures look flipped vertically, that choice
picked the wrong one of the two UV conventions; say so in an issue with the
output of `tools/mmi inspect` and it can be pinned down.

**Every tile lands on the same spot -- one heap of giant planes collapsing into
each other.** Two causes, both now handled, and the import log tells you which
one applied:

* *The matrix layout.* An HLSL shader multiplies row vectors (`mul(pos, M)`),
  a GLSL one column vectors (`M * pos`), so the same 16 numbers describe
  transposed matrices. The importer was written for D3D11 captures and read
  every matrix the HLSL way; on a GL capture that puts the tile's translation
  where Blender ignores it, and every tile ends up at the origin. The layout
  is now decided per capture: from the capture's API, then confirmed by the
  data (a placement matrix is affine, so only one reading has `0 0 0 1` as its
  bottom row). The log prints `Matrix layout: rows/columns (capture API ...,
  affine votes ...)`.
* *The wrong matrix.* A shader may hold both a per-tile placement matrix and a
  camera matrix shared by every tile; picking the shared one places every
  tile identically. The scraper and importer now prefer the matrix whose
  value **changes between draw calls**, and the log says `(per-tile)` or
  `(shared by all tiles!)` next to the one it chose. If it is the shared one,
  the placement is not in a matrix at all -- run `tools/mmi inspect --values
  --source` and report the vertex shader.

**The geometry is right but every tile is black.** The material has an image,
so a PNG was saved -- and it is black. The scraper used to save "the last
texture bound to the fragment shader", which is fine while the shader binds
exactly one; as soon as it binds a second (a placeholder, a lookup table, a
depth texture) that is the one that got saved. It now ranks the bound
textures by size and format, saves the best one, and checks the PNG is not a
flat colour before accepting it (a real tile does not compress to a few
hundred bytes; a black one does), moving on to the next candidate otherwise.
Alpha is discarded on save: an unused, all-zero alpha channel used to make the
whole texture transparent, which Blender shows as black.

More to the point for Google Maps: its tiles are **BC1 (DXT1) compressed**,
and on an OpenGL replay RenderDoc's `SaveTexture()` reports success for such
a texture and writes an image of the right size that is entirely black --
every PNG comes out a few hundred bytes -- while `GetTextureData()` returns
the compressed blocks intact. So BC1/BC2/BC3 textures are now decoded by the
add-on itself (`bcdecode.py`, numpy only) from the raw bytes, and written as
PNG directly. The import log reports which path wrote each texture
(`Texture choice for drawcall 0: bound slot 0 of 1, 256x512 BC1_UNORM, 61234
bytes, written by our decoder, from the replay`) and ends with `Textures: N
saved, N blank, N draw calls without one`.

**Still black, and every decoded PNG is exactly 2324 bytes** (for 512x512
tiles; 1180 for 256x512). That is the size of a 512x512 image of nothing:
the replay handed the decoder a texture that is all zero. The GPU replay of
the capture, on that machine's driver, does not put the browser's uploads
back into the texture -- while the capture file itself still records every
one of them: the `glCompressedTexImage2D` / `glCompressedTexSubImage2D`
calls the browser made when the tile arrived, and RenderDoc's own snapshot
of the texture at the start of the frame. So when the replay returns an
all-zero texture, the add-on now reads the tile straight out of the capture
file (`rdtexfile.py`: the file's structured data, no GPU involved), replays
those uploads itself in order up to the draw call, and decodes that. The
log then says `written by our decoder, from the capture file` and the
summary counts how many tiles took that route. Should even the file hold
nothing for a texture, the log says so, with what the file does hold, and
the remaining option is to capture again with `tools/mmi up --api gles`:
on OpenGL ES RenderDoc keeps its own copy of every compressed upload rather
than reading the texture back from the driver. `tools/mmi inspect
--textures` prints both what the replay returns for each bound texture and
what the capture file records for it.

**The browser opens but `tools/mmi status` never shows a graphics API.** The
GPU process is not going through a path RenderDoc hooks. Try `--api gl-egl`,
then `--api gles`, then `--api vulkan`, then `tools/mmi up --in-process-gpu`, which runs the GPU
code inside the browser process so that no child has to be hooked at all.

**The capture is empty, or the import says no relevant draw calls.** You were
not moving in the 3D view (Google Maps), or the page is not in 3D mode. Check
that the view is in satellite mode with the globe/3D toggle on, and try
appending `?force=webgl` to the Maps URL.

**Nothing imports and the log mentions the renderdoc module.** Run
`tools/mmi doctor`. If the module cannot be imported, the build is either
missing or was built for a different python; `tools/mmi setup --skip-apt`
rebuilds it.

**Blender does not see the add-on after `mount`.** Some builds do not follow
symlinks into the extensions directory. `tools/mmi mount --copy` copies the
add-on instead (re-run it after changing the sources).

Configuration
-------------

`setup` writes `~/.config/maps-models-importer/config`, a plain shell file that
the script sources. Anything in it can also be given as an environment
variable:

| Variable | Meaning |
| --- | --- |
| `MMI_HOME` | Where captures, outputs and the RenderDoc build live |
| `MMI_PYTHON` | Interpreter that can import the renderdoc module |
| `MMI_BROWSER` | Browser executable |
| `MMI_BLENDER` | Blender executable |
| `MMI_API` | `gl`, `gl-egl`, `gles` or `vulkan` |
| `MMI_URL` | Page to open |
| `RENDERDOC_VERSION` | RenderDoc tag to build |
| `CHROME_VERSION` | Chrome for Testing version installed by `install-chromium` |

The add-on itself reads two environment variables, so it can be pointed at a
RenderDoc module without going through the preferences UI:
`MAPSMODELSIMPORTER_PYTHON` and `MAPSMODELSIMPORTER_RENDERDOC_MODULE_DIR`.

Using the add-on without `mmi`
------------------------------

Nothing in the add-on depends on these scripts. If you already have a
`renderdoc` python module, just set *RenderDoc Module Directory* and
*Python Executable* in the add-on preferences (`Edit > Preferences >
Add-ons > Maps Models Importer`) and import captures the usual way.
