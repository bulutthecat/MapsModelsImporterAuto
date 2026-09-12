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

"""Launch a browser with RenderDoc attached, and drive captures from a script.

RenderDoc's "inject into a running process" feature does not exist on Linux,
which is why the usual Google Maps capture recipe is Windows only. Launching
the process ourselves does work though, and with the "capture child processes"
option RenderDoc follows the browser into its GPU process, which is the one
that actually talks to the driver.

That is what this script does: it launches the browser through
``ExecuteAndInject``, keeps a target control connection to every process
RenderDoc hooks, and triggers captures on the one that has initialised a
graphics API. Captures are reported on stdout, and optionally piped straight
into Blender.

It must run under an interpreter that can import the ``renderdoc`` module;
``mmi`` takes care of that.
"""

import argparse
import errno
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

try:
    import renderdoc as rd
except ImportError as err:  # pragma: no cover - depends on the environment
    sys.stderr.write(
        "Could not import the renderdoc python module: {}\n"
        "Run `tools/mmi setup` first, and use `tools/mmi launch` rather than\n"
        "calling this script directly.\n".format(err)
    )
    sys.exit(20)


# -----------------------------------------------------------------------------
# Logging

START_TIME = time.time()

# How often to look for freshly hooked processes, in seconds. Enumerating
# blocks for a moment, so this trades a little responsiveness for not spinning.
DISCOVERY_INTERVAL = 2.0

# How long to leave a target alone after failing to connect to it.
REFUSAL_TIMEOUT = 30.0

def log(message):
    sys.stdout.write("[{:7.1f}s] {}\n".format(time.time() - START_TIME, message))
    sys.stdout.flush()


# -----------------------------------------------------------------------------
# Command channel
#
# A named pipe is the least surprising way to talk to a process that is already
# running in another terminal: `echo capture > fifo` is something a user can do
# by hand, and `mmi capture` does exactly that.

class ControlChannel:
    def __init__(self, path):
        self.path = path
        self.commands = queue.Queue()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self.path is None:
            return
        if os.path.exists(self.path):
            os.remove(self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        os.mkfifo(self.path, 0o600)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        log(f"Listening for commands on {self.path}")

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                # Opening a fifo for reading blocks until a writer shows up,
                # and reading returns as soon as that writer is gone; hence the
                # outer loop.
                with open(self.path, "r") as fifo:
                    for line in fifo:
                        line = line.strip()
                        if line:
                            self.commands.put(line)
            except OSError as err:
                if err.errno != errno.EINTR:
                    return

    def poll(self):
        try:
            return self.commands.get_nowait()
        except queue.Empty:
            return None

    def stop(self):
        self._stop.set()
        if self.path is not None and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass


# -----------------------------------------------------------------------------
# Capture options

def makeCaptureOptions(hook_children=True):
    opts = rd.CaptureOptions()
    # The whole point: the browser process itself never touches the GPU, its
    # GPU child process does.
    opts.hookIntoChildren = hook_children
    opts.allowVSync = True
    opts.allowFullscreen = True
    opts.apiValidation = False
    opts.captureCallstacks = False
    opts.debugOutputMute = True
    opts.delayForDebugger = 0
    opts.refAllResources = False
    opts.verifyBufferAccess = False
    return opts


def makeEnvironmentModifications(pairs):
    mods = []
    for name, value in pairs:
        mods.append(
            rd.EnvironmentModification(rd.EnvMod.Set, rd.EnvSep.NoSep, name, value)
        )
    return mods


# -----------------------------------------------------------------------------
# Target control

class TargetSet:
    """The set of hooked processes we are connected to.

    RenderDoc hands out one "ident" per hooked process, and allows a single
    target control connection per ident, so the browser's process tree turns
    into a handful of connections we have to pump regularly.
    """

    def __init__(self, client_name="MapsModelsImporter"):
        self.client_name = client_name
        self.controls = {}
        self.apis = {}
        # Idents we failed to connect to, and when. RenderDoc reuses idents as
        # processes come and go, so a refusal has to expire or we would end up
        # ignoring the very process we are waiting for.
        self.refused = {}

    def connect(self, ident, force=False):
        if ident in self.controls or ident == 0:
            return None
        refused_at = self.refused.get(ident)
        if refused_at is not None and time.time() - refused_at < REFUSAL_TIMEOUT:
            return None
        control = rd.CreateTargetControl("", ident, self.client_name, force)
        if control is None:
            # Most likely another RenderDoc client already owns this process.
            log(f"Could not connect to target {ident:#x}, will retry later")
            self.refused[ident] = time.time()
            return None
        self.refused.pop(ident, None)
        self.controls[ident] = control
        log(f"Connected to '{control.GetTarget()}' (pid {control.GetPID()}, ident {ident:#x})")
        return control

    def discover(self):
        """Connect to every hooked process we are not already talking to.

        On Windows RenderDoc tells the parent's connection about each child it
        hooks, but on Linux no such message is sent: the child just starts
        listening on an ident of its own. Since the process we care about --
        the browser's GPU process -- is precisely such a child, polling the
        list of live targets is the only way to find it.
        """
        found = []
        ident = 0
        while True:
            ident = rd.EnumerateRemoteTargets("", ident)
            if ident == 0:
                break
            found.append(ident)

        for ident in found:
            self.connect(ident)
        return found

    def disconnectAll(self):
        for control in self.controls.values():
            try:
                control.Shutdown()
            except Exception:
                pass
        self.controls.clear()

    def graphicsTargets(self):
        """Connections whose process has initialised a graphics API, i.e. the
        ones a capture can actually be triggered on."""
        result = []
        for ident, control in self.controls.items():
            api = control.GetAPI()
            if api:
                result.append((ident, control, api))
        return result

    def pump(self, on_capture):
        """One round of message handling over every connection.

        Returns False once every connection is gone, which is how we notice
        that the browser exited.
        """
        for ident in list(self.controls):
            control = self.controls[ident]
            if not control.Connected():
                log(f"Target {ident:#x} disconnected")
                control.Shutdown()
                del self.controls[ident]
                continue

            msg = control.ReceiveMessage(None)
            msg_type = msg.type

            if msg_type == rd.TargetControlMessageType.Noop:
                continue
            if msg_type == rd.TargetControlMessageType.Disconnected:
                log(f"Target {ident:#x} disconnected")
                control.Shutdown()
                del self.controls[ident]
            elif msg_type == rd.TargetControlMessageType.NewChild:
                child = msg.newChild
                log(f"Browser spawned child process {child.processId}")
                self.connect(child.ident)
            elif msg_type == rd.TargetControlMessageType.RegisterAPI:
                api = msg.apiUse
                state = "capturable" if api.supported else f"NOT capturable: {api.supportMessage}"
                log(f"Target {ident:#x} initialised {api.name} ({state})")
                self.apis[ident] = api.name
            elif msg_type == rd.TargetControlMessageType.NewCapture:
                on_capture(control, msg.newCapture)
            elif msg_type == rd.TargetControlMessageType.CaptureProgress:
                if msg.capProgress >= 0.0:
                    log(f"Capturing... {msg.capProgress * 100.0:.0f}%")
            elif msg_type == rd.TargetControlMessageType.Busy:
                log(f"Target {ident:#x} is busy, held by {control.GetBusyClient()}")

        return bool(self.controls)


# -----------------------------------------------------------------------------

class Session:
    def __init__(self, args):
        self.args = args
        self.targets = TargetSet()
        self.channel = ControlChannel(args.control_fifo)
        self.pending_captures = 0
        self.stop = False
        self.capture_count = 0
        self.hooks = []

    # -- capture handling --------------------------------------------------

    def onNewCapture(self, control, capture):
        path = capture.path
        if not capture.local:
            # Only happens for remote targets, but harmless to support.
            path = os.path.join(self.args.capture_dir, f"remote_{capture.captureId}.rdc")
            control.CopyCapture(capture.captureId, path)

        destination = self.uniqueDestination(capture)
        try:
            os.makedirs(self.args.capture_dir, exist_ok=True)
            shutil.move(path, destination)
        except OSError as err:
            log(f"Could not move the capture to {destination}: {err}")
            destination = path

        self.capture_count += 1
        log(f"Capture saved: {destination} ({capture.byteSize / (1024 * 1024):.1f} MB)")

        if self.args.on_capture:
            self.runHook(destination)

    def uniqueDestination(self, capture):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"capture-{stamp}-frame{capture.frameNumber}"
        destination = os.path.join(self.args.capture_dir, base + ".rdc")
        index = 1
        while os.path.exists(destination):
            destination = os.path.join(self.args.capture_dir, f"{base}-{index}.rdc")
            index += 1
        return destination

    def runHook(self, capture_path):
        command = shlex.split(self.args.on_capture) + [capture_path]
        log("Running " + " ".join(shlex.quote(c) for c in command))
        try:
            self.hooks.append(subprocess.Popen(command))
        except OSError as err:
            log(f"Could not run the capture hook: {err}")

    def reapHooks(self):
        """Collect finished capture hooks so they do not pile up as zombies
        over a long session."""
        still_running = []
        for hook in self.hooks:
            if hook.poll() is None:
                still_running.append(hook)
            elif hook.returncode != 0:
                log(f"A capture hook failed with exit code {hook.returncode}")
        self.hooks = still_running

    # -- commands ----------------------------------------------------------

    def triggerCapture(self, frames=1):
        targets = self.targets.graphicsTargets()
        if not targets:
            log(
                "No hooked process has initialised a graphics API yet. Make sure "
                "the browser is displaying 3D content, then try again."
            )
            return
        for ident, control, api in targets:
            log(f"Triggering a {frames} frame capture on {api} target {ident:#x}")
            control.TriggerCapture(frames)

    def handleCommand(self, command):
        parts = command.split()
        name = parts[0].lower()
        if name in ("capture", "c"):
            frames = int(parts[1]) if len(parts) > 1 else 1
            self.triggerCapture(frames)
        elif name == "status":
            targets = self.targets.graphicsTargets()
            log(f"{len(self.targets.controls)} hooked process(es), {len(targets)} with a live API")
            for ident, control, api in targets:
                log(f"  - {control.GetTarget()} (pid {control.GetPID()}): {api}")
            log(f"{self.capture_count} capture(s) taken so far")
        elif name in ("quit", "stop", "exit"):
            log("Stopping on request")
            self.stop = True
        else:
            log(f"Unknown command: {command}")

    # -- main loop ---------------------------------------------------------

    def run(self):
        args = self.args
        os.makedirs(args.capture_dir, exist_ok=True)

        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        try:
            return self._run()
        finally:
            self.targets.disconnectAll()
            self.channel.stop()
            rd.ShutdownReplay()

    def _run(self):
        args = self.args

        env_mods = makeEnvironmentModifications(
            [tuple(e.split("=", 1)) for e in args.env if "=" in e]
        )
        opts = makeCaptureOptions(hook_children=not args.no_hook_children)
        cmdline = " ".join(shlex.quote(a) for a in args.browser_args)
        capture_template = os.path.join(args.capture_dir, "rdoc")

        log(f"Launching {args.browser} {cmdline}")
        result = rd.ExecuteAndInject(
            args.browser,      # app
            "",                # working dir
            cmdline,           # command line
            env_mods,          # environment
            capture_template,  # capture file template
            opts,              # capture options
            False,             # do not block
        )

        if result.ident == 0:
            log(f"Failed to launch the browser: {result.result}")
            return 1

        self.targets.connect(result.ident, force=True)
        self.targets.discover()
        self.channel.start()

        log("")
        log("The browser is running with RenderDoc attached.")
        log("  - Navigate to the 3D view you want, then take a capture with")
        log("    `tools/mmi capture` from another terminal (or press F12 in the")
        log("    browser window).")
        log("  - For Google Maps you must be MOVING in the 3D view at the very")
        log("    moment the capture is taken; `tools/mmi capture --delay 5` gives")
        log("    you time to start dragging.")
        log("  - Press Ctrl+C here to stop the session.")
        log("")

        signal.signal(signal.SIGINT, self._onInterrupt)
        signal.signal(signal.SIGTERM, self._onInterrupt)

        last_discovery = time.time()
        while not self.stop:
            command = self.channel.poll()
            if command is not None:
                self.handleCommand(command)

            if time.time() - last_discovery > DISCOVERY_INTERVAL:
                self.targets.discover()
                last_discovery = time.time()

            self.reapHooks()

            if not self.targets.pump(self.onNewCapture):
                log("The browser exited.")
                break

        log(f"Session over, {self.capture_count} capture(s) taken.")
        return 0

    def _onInterrupt(self, signum, frame):
        if self.stop:
            # A second Ctrl+C means business.
            raise KeyboardInterrupt
        log("Interrupted, shutting down (Ctrl+C again to force)")
        self.stop = True


# -----------------------------------------------------------------------------

def parseArgs(argv):
    parser = argparse.ArgumentParser(
        description="Launch a browser under RenderDoc and drive captures from a script",
    )
    parser.add_argument("--browser", required=True, help="Path to the browser executable")
    parser.add_argument(
        "--browser-arg",
        dest="browser_args",
        action="append",
        default=[],
        help="Argument to pass to the browser, repeatable",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Environment variable to set in the browser, repeatable",
    )
    parser.add_argument("--capture-dir", required=True, help="Where to store captures")
    parser.add_argument("--control-fifo", default=None, help="Named pipe to read commands from")
    parser.add_argument(
        "--on-capture",
        default=None,
        help="Command to run for each new capture, with the capture path appended",
    )
    parser.add_argument(
        "--no-hook-children",
        action="store_true",
        help="Do not follow the browser into its child processes (you almost "
             "certainly want the default, since the GPU process is a child)",
    )
    return parser.parse_args(argv)


def main(argv):
    return Session(parseArgs(argv)).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
