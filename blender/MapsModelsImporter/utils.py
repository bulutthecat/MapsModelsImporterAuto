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

import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile

# -----------------------------------------------------------------------------

# Environment variables that can be used to point the add-on at a RenderDoc
# module and at an interpreter able to load it, without going through the
# add-on preferences. Handy for scripted/headless setups.
ENV_PYTHON = "MAPSMODELSIMPORTER_PYTHON"
ENV_MODULE_DIR = "MAPSMODELSIMPORTER_RENDERDOC_MODULE_DIR"

# Interpreters we try, in order, when nothing was configured explicitly.
# Newest first: the renderdoc module is usually built against the distribution
# python, and that is what these resolve to.
CANDIDATE_PYTHONS = (
    "python3",
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3.9",
    "python",
)

# -----------------------------------------------------------------------------

def randomHash(length=7):
	alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
	return ''.join([random.choice(alphabet) for i in range(length)])

# -----------------------------------------------------------------------------

def getBinaryDir():
	"""Directory of the RenderDoc binaries shipped with the add-on, if any.
	This is platform specific, and may well not exist: on Linux and macOS no
	binary is bundled, the module is expected to be provided by the user (see
	getRenderdocModuleDirs)."""
	platform_dir = {
		"Windows": "win",
		"Linux": "linux",
		"Darwin": "macos",
	}.get(platform.system(), platform.system().lower())
	bitness = "64" if platform.architecture()[0] == "64bit" else "32"
	return os.path.join(os.path.dirname(os.path.realpath(__file__)), "bin", platform_dir + bitness)

# -----------------------------------------------------------------------------

def getRenderdocModuleDirs(pref=None):
	"""All the directories in which the renderdoc python module may live, in
	decreasing order of priority. The list is used to build the PYTHONPATH of
	the extraction subprocess, so it is fine for entries not to exist."""
	dirs = []

	def add(directory):
		if not directory:
			return
		directory = os.path.abspath(bpy_path_abspath(directory))
		if directory not in dirs:
			dirs.append(directory)

	if pref is not None:
		add(getattr(pref, "renderdoc_module_dir", ""))
	add(os.environ.get(ENV_MODULE_DIR, ""))
	add(getBinaryDir())
	return dirs

# -----------------------------------------------------------------------------

def bpy_path_abspath(path):
	"""Resolve Blender's '//'-relative paths when bpy is available, and expand
	the user's home directory. Kept tolerant so that this module stays usable
	outside of Blender."""
	if not path:
		return path
	try:
		import bpy
		path = bpy.path.abspath(path)
	except ImportError:
		pass
	return os.path.expanduser(path)

# -----------------------------------------------------------------------------

def canImportRenderdoc(python, module_dirs):
	"""Check whether the given interpreter can import both renderdoc and numpy
	with the given directories added to its path."""
	env = dict(os.environ)
	env.pop("PYTHONHOME", None)
	env["PYTHONPATH"] = os.pathsep.join(
		[d for d in module_dirs if d] + [env.get("PYTHONPATH", "")]
	).strip(os.pathsep)
	try:
		subprocess.run(
			[python, "-c", "import renderdoc, numpy"],
			env=env,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			timeout=20,
			check=True,
		)
	except (OSError, subprocess.SubprocessError):
		return False
	return True

# -----------------------------------------------------------------------------

def findPython(pref=None, module_dirs=None):
	"""Find an interpreter able to load the renderdoc module.

	The first of these that works wins:
	  1. the "Python Executable" add-on preference;
	  2. the MAPSMODELSIMPORTER_PYTHON environment variable;
	  3. Blender's own interpreter (this is what makes Windows work out of the
	     box, since the bundled module is built for it);
	  4. any python3 found in PATH.

	Returns (python_executable, was_explicitly_configured). When nothing can
	import the module we still return Blender's interpreter so that the caller
	produces the usual "module not found" diagnostic.
	"""
	if module_dirs is None:
		module_dirs = getRenderdocModuleDirs(pref)

	explicit = []
	if pref is not None:
		explicit.append(bpy_path_abspath(getattr(pref, "python_exe", "")))
	explicit.append(os.environ.get(ENV_PYTHON, ""))
	for python in explicit:
		if python:
			return python, True

	candidates = [sys.executable]
	for name in CANDIDATE_PYTHONS:
		found = shutil.which(name)
		if found is not None and found not in candidates:
			candidates.append(found)

	for python in candidates:
		if canImportRenderdoc(python, module_dirs):
			return python, False

	return sys.executable, False

# -----------------------------------------------------------------------------

def makeTmpDir(pref, filepath=None):
	"""Create a temporary directory in the tmp dir specified in preferences. filepath can be specified to hint the name.
	@return prefix, with the temporary dir plus a prefix if filepath was provided"""
	prefix = ""
	if filepath is not None:
		prefix = os.path.splitext(os.path.basename(filepath))[0] + "-"
	parent = bpy_path_abspath(pref.tmp_dir)
	if not parent:
		if filepath is not None:
			parent = os.path.dirname(filepath)
		else:
			parent = tempfile.gettempdir()
	base = os.path.join(parent, prefix + randomHash(7))
	while os.path.isdir(base):
		base = os.path.join(parent, prefix + randomHash(7))
	os.makedirs(base)
	return os.path.join(base, prefix)
