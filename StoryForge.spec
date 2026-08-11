# -*- mode: python ; coding: utf-8 -*-
# Authoritative PyInstaller specification for StoryForge v1.x releases.

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


project_root = Path(SPECPATH).resolve()
bundle_local_ai = os.environ.get("STORYFORGE_BUNDLE_LOCAL_AI") == "1"
build_onefile = os.environ.get("STORYFORGE_BUILD_MODE") == "onefile"

datas = [
    (str(project_root / "ui"), "ui"),
]
datas += collect_data_files("imageio_ffmpeg", include_py_files=False)
binaries = []

# pywebview chooses its Windows backend dynamically, so make the selected
# Edge WebView2 modules explicit to PyInstaller.
hiddenimports = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]

if bundle_local_ai:
    # Kokoro is intentionally optional. Collecting it (and the dependencies
    # found through it) produces a much larger executable.
    hiddenimports += collect_submodules("kokoro")
    hiddenimports += collect_submodules("en_core_web_sm")
    # Kokoro imports language G2P stacks dynamically.  In particular,
    # collecting only ``unidic_lite.__init__`` leaves Japanese apparently
    # installed but crashes at runtime when it opens ``dicdir/version``.
    for package in (
        "pyopenjtalk",
        "fugashi",
        "jaconv",
        "mojimoji",
        "unidic_lite",
        "jieba",
        "ordered_set",
        "pypinyin",
        "cn2an",
        "pypinyin_dict",
    ):
        hiddenimports += collect_submodules(package)
    datas += collect_data_files("kokoro", include_py_files=False)
    datas += collect_data_files("unidic_lite", include_py_files=False)
    datas += collect_data_files("pypinyin_dict", include_py_files=False)
    # Misaki uses espeakng-loader for English fallback phonemization. PyInstaller
    # does not discover the loader's DLL or pronunciation tables automatically.
    datas += collect_data_files("espeakng_loader", include_py_files=False)
    binaries += collect_dynamic_libs("espeakng_loader")
    # Kokoro's English G2P stack opens these package resources at runtime.
    # They are indirect dependencies, so PyInstaller sees their Python modules
    # but does not collect the JSON/lexicon/profile files automatically.
    datas += collect_data_files("language_tags", include_py_files=False)
    datas += collect_data_files("misaki", include_py_files=False)
    datas += collect_data_files("phonemizer", include_py_files=False)
    # spaCy checks distribution metadata before loading the bundled model.
    # Without this, a frozen app can incorrectly try to download the model.
    datas += copy_metadata("en-core-web-sm")
    datas += copy_metadata("phonemizer-fork")
    datas += copy_metadata("espeakng-loader")
    datas += copy_metadata("unidic-lite")
    # Kokoro otherwise tries to install this model dynamically on first use,
    # which cannot work reliably from a frozen single-file application.
    datas += collect_data_files("en_core_web_sm", include_py_files=False)

a = Analysis(
    [str(project_root / "run.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

if build_onefile:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="StoryForge Studio",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    # The production build defaults to onedir.  The former 397 MB onefile had
    # to unpack more than 1 GB before Python could display a window or an error,
    # which looked like a dead application on lower-spec workstations.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="StoryForge Studio",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="StoryForge Studio",
    )
