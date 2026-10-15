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
# 용량 줄이기: 쓰지 않는 대용량 바이너리/데이터 제거
_DROP_BIN = ("opencv_videoio_ffmpeg", "opengl32sw", "d3dcompiler", "Qt5Quick", "Qt5Qml", "Qt5Network", "Qt5Svg",
             "Qt5DBus", "Qt5WebSockets", "_avif", "libGLESv2", "libEGL")
_DROP_DATA = ("ch_PP-OCRv4_rec_infer.onnx", "translations")   # 기본 중국어 rec 모델 등
# PyQt5 가 가져온 오래된 VC++ 런타임(14.26)이 먼저 로드되면 onnxruntime 이 시작 시 access violation 으로 죽는다.
# 번들에서 빼면 같은 폴더의 최신 런타임(msvcp140.dll 등)을 Qt 와 onnxruntime 이 함께 쓴다.
def _old_qt_vcrt(name):
    n = name.lower().replace("\\", "/")
    return "pyqt5/" in n and (n.rsplit("/", 1)[-1].startswith(("msvcp140", "vcruntime140", "concrt140")))


a.binaries = [b for b in a.binaries if not any(k in b[0] for k in _DROP_BIN) and not _old_qt_vcrt(b[0])]
a.datas = [d for d in a.datas if not any(k in d[0] for k in _DROP_DATA)]

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