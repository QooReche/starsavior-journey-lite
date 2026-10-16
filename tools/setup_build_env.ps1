# 빌드/테스트 환경(C:\build\jh) 생성 스크립트. 사용: powershell -File tools\setup_build_env.ps1
# 한글 경로에서는 PyInstaller/Qt 가 문제를 일으키므로 ASCII 경로(C:\build\jh)에 소스 사본과 venv 를 둔다.
param([string]$Env = "C:\build\jh")
$py = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
$here = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force $Env | Out-Null
robocopy $here "$Env\src" /E /XD .git .venv __pycache__ .pytest_cache build dist /NFL /NDL /NJH /NJS | Out-Null   # robocopy 종료 코드 1 = 복사됨(정상)
& $py -m venv "$Env\.venv"
& "$Env\.venv\Scripts\python.exe" -m pip install -r "$Env\src\requirements-dev.txt"
# 중요: PyInstaller 분석 단계가 PyQt5 를 먼저 import 하면 onnxruntime 이 access violation 으로 죽는다 -> onnxruntime 을 먼저 import 시키는 .pth
$site = & "$Env\.venv\Scripts\python.exe" -c "import sysconfig;print(sysconfig.get_paths()['purelib'])"
Set-Content -Encoding ascii "$site\zz_ort_first.pth" "import onnxruntime"
Push-Location "$Env\src"; & "$Env\.venv\Scripts\python.exe" -m pytest -q; Pop-Location
Write-Host "빌드: $Env\.venv\Scripts\pyinstaller.exe journey_helper.spec --noconfirm --distpath $Env\dist --workpath $Env\build (cwd=$Env\src)"
