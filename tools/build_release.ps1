# 테스트 -> 빌드 -> 배포용 zip/해시 -> (선택) release 배포·재실행 을 한 번에 한다.
# 사용: powershell -File tools\build_release.ps1 [-Deploy] [-UpdateHandoff] [-SkipTests]
#   -Deploy        앱을 종료하고 빌드 결과를 ..\release\JourneyHelperLite 에 덮어쓴 뒤 다시 실행한다(data\ log\ 는 건드리지 않음).
#   -UpdateHandoff HANDOFF.md 의 'sha256 `...`' 값을 새 zip 해시로 바꾼다(나머지 문장은 직접 갱신).
#   -SkipTests     pytest 를 건너뛴다(권장하지 않음).
# 순서/규칙(HANDOFF.md '빌드 절차'): src -> C:\build\jh\src 동기화 -> 날짜 스탬프 -> pytest(결과 확인, 실패하면 중단) -> PyInstaller
#   -> make_release_zip.ps1(실행해 본 적 없는 dist 만 압축) -> sha256.txt 갱신 -> (선택) 배포.
# 빌드 환경(C:\build\jh\.venv)이 없으면 tools\setup_build_env.ps1 로 먼저 만든다.
param(
    [switch]$Deploy,
    [switch]$UpdateHandoff,
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$src = Split-Path $PSScriptRoot -Parent
$build = "C:\build\jh"
$bsrc = "$build\src"
$py = "$build\.venv\Scripts\python.exe"
$dist = "$build\dist"
$out = "$build\out"
$release = Join-Path (Split-Path $src -Parent) "release\JourneyHelperLite"
if (-not (Test-Path $py)) { throw "빌드 환경이 없습니다: tools\setup_build_env.ps1 를 먼저 실행하세요." }

function Step($t) { Write-Output ""; Write-Output "== $t" }
function RoboCopy-Tree($from, $to, [string[]]$extra) {
    robocopy $from $to /E @extra /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 실패 (종료 코드 $LASTEXITCODE): $from -> $to" }   # 0~7 은 정상(복사됨/추가 파일 있음)
}

Step "소스 동기화"
RoboCopy-Tree $src $bsrc @("/XD", ".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist")

Step "버전 날짜 스탬프"
Push-Location $bsrc
try {
    & $py -X utf8 tools\stamp_version.py
    # 스탬프는 빌드 사본만 바꾸므로 src 의 jh_version.py 도 같은 값으로 맞춘다(안 맞추면 다음 동기화에서 되돌아감)
    $a = Get-Content "$bsrc\jh_version.py" -Raw -Encoding utf8
    $b = Get-Content "$src\jh_version.py" -Raw -Encoding utf8
    if ($a -ne $b) { Copy-Item "$bsrc\jh_version.py" "$src\jh_version.py" -Force; Write-Output "src\jh_version.py 를 빌드 값으로 갱신했습니다(커밋 필요)." }

    if (-not $SkipTests) {
        Step "pytest"
        $last = & $py -m pytest -q -p no:cacheprovider 2>&1 | Select-Object -Last 1
        Write-Output $last
        if ($last -notmatch "passed" -or $last -match "failed|error") { throw "테스트가 통과하지 않아 중단합니다." }
    }

    Step "PyInstaller"
    if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
    & "$build\.venv\Scripts\pyinstaller.exe" journey_helper.spec --noconfirm --distpath $dist --workpath "$build\build" 2>&1 | Select-Object -Last 1
    if (-not (Test-Path "$dist\JourneyHelperLite\JourneyHelperLite.exe")) { throw "빌드 결과가 없습니다." }

    Step "zip / sha256"
    powershell -NoProfile -File tools\make_release_zip.ps1 2>&1 | Select-Object -Last 1
} finally { Pop-Location }

$zip = Get-ChildItem $out -Filter "JourneyHelperLite_*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$hash = (Get-FileHash $zip.FullName -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText("$($zip.FullName).sha256.txt", "$hash  $($zip.Name)`n", (New-Object Text.UTF8Encoding $false))
Write-Output "zip    : $($zip.FullName)"
Write-Output "sha256 : $hash"

if ($UpdateHandoff) {
    $h = Join-Path (Split-Path $src -Parent) "HANDOFF.md"
    $t = Get-Content $h -Raw -Encoding utf8
    $n = [regex]::Replace($t, 'sha256 `[0-9a-f]{64}`', "sha256 ``$hash``", 1)
    if ($n -ne $t) { [IO.File]::WriteAllText($h, $n, (New-Object Text.UTF8Encoding $false)); Write-Output "HANDOFF.md sha256 갱신" }
    else { Write-Output "HANDOFF.md 에서 갱신할 sha256 줄을 찾지 못했습니다(변경 없음 또는 형식 다름)." }
}

if ($Deploy) {
    Step "배포(release) 및 재실행"
    Get-Process JourneyHelperLite -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep 2
    RoboCopy-Tree "$dist\JourneyHelperLite" $release @()
    Start-Process -FilePath "$release\JourneyHelperLite.exe" -WorkingDirectory $release
    Start-Sleep 8
    $p = Get-Process JourneyHelperLite -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($p) { Write-Output "실행 중: PID $($p.Id)" } else { Write-Output "경고: 앱이 실행되지 않았습니다." }
    $log = "$release\log\log.txt"
    if (Test-Path $log) { Get-Content $log -Encoding utf8 -Tail 2 }
}
Write-Output ""
Write-Output "완료. 변경 사항은 git 으로 커밋하세요(스크립트는 커밋/푸시를 하지 않습니다)."
