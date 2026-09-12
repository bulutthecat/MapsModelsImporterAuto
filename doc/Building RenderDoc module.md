Building the RenderDoc python module
====================================

The add-on reads capture files through RenderDoc's python module, which
RenderDoc does not ship: it has to be built from source against one specific
python version. Since version 0.8.0 the add-on can load it with any
interpreter that also has numpy, so it no longer has to match the python
version bundled with Blender.

Linux
-----

`tools/mmi setup` does all of this for you. If you would rather do it by
hand, it comes down to:

```
sudo apt-get install build-essential cmake git pkg-config \
    libx11-dev libx11-xcb-dev libxcb-keysyms1-dev \
    mesa-common-dev libgl1-mesa-dev \
    bison autoconf automake libpcre3-dev python3-dev python3-numpy

git clone --depth 1 --branch v1.46 https://github.com/baldurk/renderdoc.git
cmake -S renderdoc -B renderdoc/build \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_QRENDERDOC=OFF \
    -DENABLE_PYRENDERDOC=ON \
    -DFORCE_PY_VERSION=3.12
cmake --build renderdoc/build --parallel
```

`ENABLE_QRENDERDOC=OFF` skips the Qt user interface, which is both the
heaviest dependency and of no use here. The build leaves `renderdoc.so` next
to `librenderdoc.so` in `renderdoc/build/lib`; keep them together, since the
module finds the library through an `$ORIGIN` rpath.

Point the add-on at that directory with the *RenderDoc Module Directory*
preference (and *Python Executable* if `python3` is not the interpreter you
built against).

Windows
-------

```
Get python source code in Python-X.X.X
Get embedable release in python-X.X.X-embed-amd64

Edit qrenderdoc/pythonXX.natvis by copying the existing pythonYY.natvis and replacing pythonYY by pythonXX everywhere in its content.

Copy Python-X.X.X\Include\*.h to qrenderdoc\3rdparty\python\include
Copy C:\PythonXX\Include\pyconfig.h to qrenderdoc\3rdparty\python\include, C:\PythonXX being the install path of PythonXX

Copy python-X.X.X-embed-amd64/pythonXX.zip to qrenderdoc\3rdparty\python
Copy python-X.X.X-embed-amd64/pythonXX.dll and _ctypes.pyd to qrenderdoc\3rdparty\python\x64

In qrenderdoc/qrenderdoc_local.vcxproj replace
<Natvis Include="python36.natvis" />
with
<Natvis Include="pythonXX.natvis" />

In qrenderdoc/Code/pyrenderdoc/python.props replace
<PythonMajorMinor>36</PythonMajorMinor>
with
<PythonMajorMinor>XX</PythonMajorMinor>

In qrenderdoc/qrenderdoc.pro replace
python36.lib
with
pythonXX.lib
(twice)

Manually run the lines of dll2lib.bat in qrenderdoc\3rdparty\python\x64

Open renderdoc.sln in VisualStudio
Right click on the solution, "Retarget Solution" to your latest
Make sure you build the x64 version, not x86, Release mode

Build pyrenderdoc_module
Copy x64/Release/renderdoc.dll and x64/Release/pymodules/renderdoc.pyd to MapsModelsImporter/blender/bin/win64
```