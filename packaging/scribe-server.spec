# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for scribe-server.exe.

Built one-dir rather than one-file. A one-file build unpacks itself into a
temporary folder on every launch - slower to start, and antivirus scanners
look hard at an executable that writes executables - and the shell ships the
whole folder as an extraResource anyway, so there is nothing to gain.

Run from the repository root:

    python -m PyInstaller packaging/scribe-server.spec --noconfirm

or ``npm run build:server`` from electron/. Output: dist/scribe-server/.

What has to be named here, because PyInstaller cannot see it by following
imports:

* The pages and the injected script, read from disk at request time.
* ``.env.example``, which a new data directory is seeded from.
* Modules imported inside functions: the LLM SDKs (only the configured one
  is imported), uvicorn's protocol and loop implementations (chosen by name
  at runtime), and every meetbot module, since several are imported lazily
  so the server starts without loading what it may never use.
* PyAudioWPatch's extension, which carries PortAudio with WASAPI loopback
  compiled in - the whole of local capture. The hook below collects its
  binaries explicitly, and the build is checked by recording from the frozen
  executable, not by trusting that it was found.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller

datas = [
    (str(ROOT / "meetbot" / "service" / "ui.html"), "meetbot/service"),
    (str(ROOT / "meetbot" / "service" / "login.html"), "meetbot/service"),
    (str(ROOT / "meetbot" / "capture" / "inject.js"), "meetbot/capture"),
    (str(ROOT / ".env.example"), "."),
]
binaries = []
hiddenimports = (
    collect_submodules("meetbot")
    + collect_submodules("uvicorn")
    + ["multipart", "python_multipart", "dotenv"]
)

# Packages whose data files or native parts have to come along whole.
for package in ("pyaudiowpatch", "anthropic", "openai", "websockets", "playwright"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:  # noqa: BLE001 - an optional package that is not installed
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "scribe_server.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # The shell draws the icon now; the pystray tray is for the from-source
    # run only, and Tk is never used.
    excludes=["tkinter", "pystray", "pytest", "IPython", "torch", "PyQt5", "PyQt6",
              "matplotlib", "pandas", "scipy", "traitlets", "jupyter_client"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

icon = ROOT / "electron" / "resources" / "icon.ico"

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="scribe-server",
    # No console window: the shell starts this hidden, and a console would
    # be a second, closable window that kills the recorder when closed.
    console=False,
    icon=str(icon) if icon.exists() else None,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="scribe-server",
)
