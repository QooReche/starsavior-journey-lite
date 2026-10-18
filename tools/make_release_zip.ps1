# 배포용 zip 생성: 한 번도 실행하지 않은 PyInstaller 빌드 결과(dist\JourneyNavigator)만 압축한다.
# 사용: powershell -File tools\make_release_zip.ps1 [-Dist C:\build\jh\dist\JourneyNavigator] [-OutDir C:\build\jh\out]
# - 개발 중 실행해서 data\ log\ 가 생긴 폴더(release\)를 압축하지 않도록, 이 폴더들이 있으면 중단한다.
# - 출력은 OneDrive 밖(C:\build\jh\out)에 둔다. 파일명은 버전을 따른다.
param(
    [string]$Dist = "C:\build\jh\dist\JourneyNavigator",
    [string]$OutDir = "C:\build\jh\out"
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

if (-not (Test-Path "$Dist\JourneyNavigator.exe")) { throw "빌드 결과가 없습니다: $Dist" }
foreach ($bad in @("data", "log")) {
    if (Test-Path "$Dist\$bad") { throw "$Dist\$bad 가 있습니다. 실행해 본 폴더는 배포하지 마세요(개인 설정/로그 혼입). 새로 빌드하세요." }
}

# 버전은 소스(jh_version.py)에서 읽는다. 빌드 사본(C:\build\jh\src)을 우선 사용한다.
$verFile = Join-Path (Split-Path $PSScriptRoot -Parent) "jh_version.py"
$build = "C:\build\jh\src\jh_version.py"
if (Test-Path $build) { $verFile = $build }
$txt = Get-Content $verFile -Raw -Encoding utf8
$base = [regex]::Match($txt, 'BASE_VERSION\s*=\s*"([^"]+)"').Groups[1].Value
$date = [regex]::Match($txt, 'BUILD_DATE\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $base -or -not $date) { throw "버전을 읽지 못했습니다: $verFile" }
$ver = "$base.$date"

New-Item -ItemType Directory -Force $OutDir | Out-Null
$zip = Join-Path $OutDir "JourneyNavigator_$ver.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
# PowerShell 5.1 의 ZipFile.CreateFromDirectory 는 경로 구분자를 역슬래시로 저장해(비표준) 일부 압축 해제기에서 폴더 구조가 깨진다.
# Windows 기본 tar(bsdtar)로 표준 zip 을 만든다.
$parent = Split-Path $Dist -Parent
$leaf = Split-Path $Dist -Leaf
& tar.exe -a -c -f $zip -C $parent $leaf
if ($LASTEXITCODE -ne 0) { throw "tar 실패 (종료 코드 $LASTEXITCODE)" }

# 검증: 경로 구분자, 개인 데이터 폴더 혼입 여부
$z = [System.IO.Compression.ZipFile]::OpenRead($zip)
try {
    $names = $z.Entries | ForEach-Object { $_.FullName }
    $back = ($names | Where-Object { $_.Contains([string][char]92) }).Count
    $leak = ($names | Where-Object { $_ -match '^[^/]+/(data|log)/' }).Count
    if ($back -gt 0) { throw "역슬래시 경로 항목이 $back 개 있습니다." }
    if ($leak -gt 0) { throw "data/log 항목이 $leak 개 들어 있습니다." }
    $exe = ($names | Where-Object { $_ -eq "$leaf/JourneyNavigator.exe" }).Count
    if ($exe -ne 1) { throw "exe 항목을 찾지 못했습니다." }
    # 라이선스 고지문(GPL-3.0/Apache-2.0/MIT 등이 배포물과 함께 사본 동봉을 요구): tools\collect_licenses.py 가 만든다
    foreach ($need in @("LICENSE", "README.md", "THIRD_PARTY_NOTICES.txt", "licenses/Apache-2.0.txt")) {
        if (-not ($names -contains "$leaf/$need")) { throw "$need 이(가) zip 에 없습니다. tools\collect_licenses.py 를 먼저 실행하세요." }
    }
    $count = $names.Count
} finally { $z.Dispose() }

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Output "made: $zip ($mb MB, $count entries), version $ver"
