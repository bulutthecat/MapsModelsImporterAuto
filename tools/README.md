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
* A browser that is **not** a snap or flatpak package. Snap confinement
  prevents RenderDoc from hooking the process. `tools/mmi install-chrome`
  installs Google Chrome from Google's apt repository, which is not confined.

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
* `--api vulkan`, which runs ANGLE on Vulkan and captures that (the RenderDoc
  Vulkan layer is registered for your user by `setup`).

Troubleshooting
---------------

**`doctor` says no browser is usable.** You most likely have the snap Chromium.
Run `tools/mmi install-chrome`, or install any non-snap Chromium build.

**The browser window opens but nothing is ever hooked.** Something reused an
already-running browser. `mmi` passes its own `--user-data-dir` to avoid that,
so also check that you have no `chrome://` policy forcing a single instance.

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
| `MMI_API` | `gl`, `gl-egl` or `vulkan` |
| `MMI_URL` | Page to open |
| `RENDERDOC_VERSION` | RenderDoc tag to build |

The add-on itself reads two environment variables, so it can be pointed at a
RenderDoc module without going through the preferences UI:
`MAPSMODELSIMPORTER_PYTHON` and `MAPSMODELSIMPORTER_RENDERDOC_MODULE_DIR`.

Using the add-on without `mmi`
------------------------------

Nothing in the add-on depends on these scripts. If you already have a
`renderdoc` python module, just set *RenderDoc Module Directory* and
*Python Executable* in the add-on preferences (`Edit > Preferences >
Add-ons > Maps Models Importer`) and import captures the usual way.
