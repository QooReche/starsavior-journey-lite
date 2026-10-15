# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir 빌드:  python -m PyInstaller journey_helper.spec --noconfirm
# 결과: dist/JourneyHelperLite/JourneyHelperLite.exe  (폴더째로 배포)
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = [("models", "models")]                       # 한국어 인식 모델 + 사전
datas += collect_data_files("rapidocr_onnxruntime")  # config.yaml, 기본 det/cls/rec 모델
binaries = collect_dynamic_libs("onnxruntime")

EXCLUDES = [
    # 원작이 쓰던 무거운 패키지 (설치돼 있어도 포함하지 않음)
    "torch", "torchvision", "easyocr", "scipy", "pandas", "matplotlib", "imagehash", "skimage", "sklearn",
    "tensorflow", "IPython", "notebook", "tkinter", "unittest", "pytest", "pydoc", "test",
    # 쓰지 않는 Qt 모듈
    "PyQt5.QtWebEngine", "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebChannel", "PyQt5.QtWebSockets",
    "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets", "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtSql", "PyQt5.QtTest", "PyQt5.QtXml", "PyQt5.QtXmlPatterns", "PyQt5.QtDesigner", "PyQt5.QtBluetooth",
    "PyQt5.QtNfc", "PyQt5.QtSensors", "PyQt5.QtSerialPort", "PyQt5.QtLocation", "PyQt5.QtPositioning",
    "PyQt5.QtOpenGL", "PyQt5.QtSvg", "PyQt5.QtPrintSupport", "PyQt5.QtHelp", "PyQt5.QtNetwork", "PyQt5.Qt3DCore",
    "PyQt5.QtDBus", "PyQt5.QtRemoteObjects", "PyQt5.QtTextToSpeech",
]

a = Analysis(
    ["journey_helper.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=["jh_db"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JourneyHelperLite",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="JourneyHelperLite")