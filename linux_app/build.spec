from pathlib import Path


source_dir = Path(SPECPATH)
repo = source_dir.parent
win_app = repo / "win_app"

analysis = Analysis(
    [str(source_dir / "beamer_linux.py")],
    # win_app before the repository root: the root's theme.py is the Mac's.
    pathex=[str(source_dir), str(win_app), str(repo)],
    binaries=[],
    datas=[(str(repo / "Beamer.png"), "."), (str(repo / "VERSION"), "."), (str(win_app / "assets"), "assets")],
    # Imported by name or only inside functions: effects.py's fx_* modules, and the Linux modules
    # beamer_linux.App and main() import late so the command-line paths stay light.
    hiddenimports=["cryptography", "nacl", "_cffi_backend", "gui", "capture", "x11", "link", "clipboard_linux", "receiver"]
    + sorted(path.stem for path in win_app.glob("fx_*.py")),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL", "tkinter", "numpy", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
              "Quartz", "AppKit", "Foundation", "objc"],
    noarchive=False,
)

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="beamer",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

COLLECT(executable, analysis.binaries, analysis.datas, name="beamer", strip=False, upx=False)
