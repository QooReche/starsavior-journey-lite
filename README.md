# 여정 헬퍼 Lite (JourneyHelper Lite)

스타세이비어(Star Savior) **여정 이벤트** 화면에서 선택지 위에 마우스를 올리면 **예상 보상**과 **잠재력/버프 설명**을 왼쪽 툴팁으로 보여주는 오버레이 도구입니다.
원작 [p1021/journeyhelper](https://github.com/p1021/journeyhelper)를 가볍게 다시 만든 포크로, 계산기류 기능 없이 "화면 인식 -> 선택지 정보 표시"만 합니다.

- torch / easyocr 없이 **rapidocr-onnxruntime**(PaddleOCR 한국어 인식 모델)을 사용합니다.
- 데이터는 [starsavior-guide.github.io](https://starsavior-guide.github.io/data/)의 공개 DB를 내려받아 로컬에 캐시합니다.
- 게임 메모리/패킷을 건드리지 않고, 화면을 읽어서(OCR) 보여주기만 합니다.

> 비공식 팬 도구입니다. 게임 및 게임 데이터의 권리는 각 권리자에게 있습니다.

## 원작 대비 달라진 점

| | 원작 | Lite |
|---|---|---|
| OCR | easyocr + torch (수 GB) | rapidocr-onnxruntime (CPU, 한국어 rec 모델 24MB) |
| 데이터 | star-savior-arcana-db (종료 예정) | starsavior-guide.github.io, **로컬 캐시 + 조건부 갱신** |
| 카드 그림 인식 / imagehash | 있음 | 없음 (동명 이벤트는 아르카나별 보상을 한 툴팁에 함께 표시) |
| 생일/인사말, 자동 보정, 디버그 오버레이, 클릭 추적 | 있음 | 제거 |
| 설정 | 큰 설정창 | 가벼운 우클릭 메뉴 |
| 추가 | | 잠재력/여정 버프/아이템 설명 표시, ko/en 화면 언어, 추천(DB 추천 선택지 표시) |

## 설치 / 실행

### 배포판(권장)
1. 릴리스의 `JourneyHelperLite` 폴더(zip)를 **통째로** 원하는 곳에 풀기
2. `JourneyHelperLite.exe` 실행 (처음엔 Windows SmartScreen 경고가 뜰 수 있습니다. 서명되지 않은 개인 제작 도구라서입니다)
3. 게임을 **창 모드 또는 테두리 없는 창 모드**로 실행 (전용 전체화면은 오버레이가 가려질 수 있습니다)

### 소스로 실행
Windows 10/11, Python 3.10~3.12 (개발/검증: 3.11)
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python journey_helper.py
```
설치/빌드 경로에 한글이 있으면 Qt 5가 멈추는 환경이 있어, 프로그램이 시작 시 플러그인을 ASCII 경로(`%LOCALAPPDATA%\JourneyHelperLite\qt_plugins`)로 복사해서 우회합니다. 가능하면 ASCII 경로를 권장합니다.

## 사용법
- 상단바는 **접힌 상태**로 시작합니다(`🔽 여정 시 펼쳐주세요`). 여정/이벤트가 감지되면 잠깐 펼쳐졌다가 자동으로 다시 접힙니다.
- **상단바 좌클릭**: 메뉴 (펼치기/접기, DB 새로고침, 일시정지, 종료) · **우클릭**: 설정 (글자 크기, 설명 표시, 시작 상태, 언어, 위치 고정/초기화) · 위치 잠금 해제 시 드래그 이동
- 여정 **이벤트 화면**에서 선택지에 마우스를 올리면 왼쪽에 툴팁이 뜹니다. 선택지가 없거나 하나뿐인(자동 진행) 이벤트와 DB에 없는 제목은 툴팁을 띄우지 않습니다(`custom_events.json`의 사용자 안내문은 예외).
- 좌측 상단 **날짜 위젯**(`3월 초순` 등)에 마우스를 올리면 그 턴의 고정/확률/랜덤 일정과 다음 고정 일정이 뜹니다(설정에서 끌 수 있음). 날짜 문구를 1초마다 읽어 턴을 판단하며, 일정 데이터는 앱에 내장되어 있습니다.
- **구원자 추천 아르카나**: 여정 시작의 아르카나 편성 화면에서 구원자(이름+부제로 동명이인 구분)를 인식해 추천 5슬롯과 대체 카드를 썸네일과 함께 보여 줍니다(디펜더 2세트는 나란히 표시). 추천 데이터는 앱에 내장되어 있고(`data/savior_reco.json`), 설정에서 끌 수 있습니다.
- 트레이 아이콘 우클릭으로도 같은 메뉴를 쓸 수 있습니다.
- 언어를 바꾸면(ko/en) DB도 해당 언어로 다시 받아야 해서 재시작을 묻습니다.

### 인식 방식
화면의 `여정 이벤트 / 훈련 이벤트 / 아르카나 이벤트` **라벨을 먼저 확인**한 경우에만 제목을 읽습니다 (장비/스탯 화면에서의 오인식 방지). 로딩 화면 이후와 이벤트 화면을 벗어났을 때는 상태를 초기화하고 라벨부터 다시 확인합니다. 모델은 게임이 감지될 때 처음 로딩됩니다.

## 업데이트 확인 정책 (DB 서버에 부담 주지 않기)
- 캐시 위치: `%APPDATA%\JourneyHelperLite\cache` (설정/로그: `%APPDATA%\JourneyHelperLite`)
- 시작할 때 **최대 12시간에 한 번만** 확인합니다 (`last_check` 저장). 그 안에 재시작해도 요청하지 않습니다.
- **조건부 GET**(`If-None-Match` / `If-Modified-Since`)을 쓰므로 바뀌지 않았으면 304로 끝납니다. 요청 대상은 JSON 2개(`journey/<언어>/journey.json`, `arcanas/arcanas.json`)뿐입니다.
- 수동 새로고침은 **10분 쿨다운**. 실패(오프라인 등) 직후 자동 재시도도 10분간 하지 않고, 조용히 캐시를 사용합니다.
- `User-Agent: JourneyHelperLite/<버전> (+프로젝트 URL)` 로 식별합니다. 응답이 깨졌거나 JSON이 아니면 기존 캐시를 덮어쓰지 않습니다.
- 첫 실행에 인터넷이 없고 캐시도 없으면 DB 없이 대기합니다 (연결 후 메뉴에서 새로고침).

## 한계 / 알려진 위험
- 이 도구는 **게임 화면 비율**(원작과 같은 좌표 비율)에 의존합니다. 게임 UI가 개편되면 영역 비율(`REGION_FRACTIONS`) 수정이 필요합니다.
- 제목은 한 줄짜리 영역을 검출 없이 바로 인식(rec-only)합니다. 제목이 두 줄이 되는 화면에서는 인식이 흔들릴 수 있습니다.
- 일반(여정) 이벤트 DB는 표(행) 형태라 선택지/보상을 행 구조(`1.` 번호, `or`, 난이도, 줄바꿈)로 복원합니다. 복잡한 표(난이도별 분기)는 줄 단위로 그대로 보여주며, 일부 표시가 DB 원문과 다를 수 있습니다. 아이템(교본, 쿠폰 등)에는 DB에 설명이 없어 이름만 표시됩니다. 설명은 **잠재력/여정 버프**만 붙습니다(새 DB에 없는 버프는 내장 보충 데이터 `jh_buffs.py` 사용). 성공/실패 확률은 어느 DB에도 없어서 표시하지 않습니다.
- 같은 제목의 아르카나 이벤트가 여러 개면 카드 썸네일을 비교해 해당 아르카나만 보여 줍니다. 식별에 실패하면(화면 전환 중 등) **아르카나별 보상을 모두** 나열합니다.
- DB(`backup-meta.json`)에 `generalJourneyFrozen: true` 표기가 있어 일반 이벤트 데이터는 고정(더 이상 갱신 안 됨)일 수 있습니다.
- 영어 모드는 영문 이벤트 제목/라벨 인식을 허용하지만, 한국어 인식 모델의 영문 정확도는 한국어보다 낮을 수 있습니다.
- 실제 게임 화면에서의 영역 정확도(라벨/제목/선택지 Y 좌표)는 게임을 켜고 확인해야 합니다.

## 개발 / 빌드
```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests -q          # DB 정규화/조건부 갱신(304)/라벨 판정/OCR 테스트
.venv\Scripts\python journey_helper.py --selftest [--no-update] [--lang en] [--out result.txt]
```
`--selftest`: DB 로드(필요 시 갱신) -> 캐시 구성 -> 이벤트 수와 샘플 툴팁 HTML 출력 -> OCR 모델 로딩 확인 후 종료.

PyInstaller (onedir):
```powershell
python -m PyInstaller journey_helper.spec --noconfirm     # dist\JourneyHelperLite\
```
- **ASCII 경로에서 빌드**하세요 (Qt 경로 조회가 한글 경로를 깨뜨려 PyInstaller Qt 훅이 실패합니다). `tools\setup_build_env.ps1` 이 `C:\build\jh` 에 소스 사본·venv·`.pth`를 만들고 테스트까지 돌립니다.
- 빌드 venv의 site-packages에 `import onnxruntime` 한 줄짜리 `.pth` 파일(`zz_ort_first.pth`)을 **반드시** 두세요. 없으면 PyInstaller 분석이 PyQt5 를 먼저 불러 onnxruntime 에서 access violation(0xC0000005)으로 죽습니다. (앱 코드도 같은 이유로 onnxruntime을 PyQt5보다 먼저 import 합니다.)
- spec이 PyQt5가 동봉한 오래된 VC++ 런타임(14.26)을 번들에서 제외합니다. 포함되면 onnxruntime이 시작 시 죽습니다.

## 크레딧 / 라이선스
- 원작: [p1021/journeyhelper](https://github.com/p1021/journeyhelper) - 화면 영역 비율, 창 탐색, 오버레이/호버 방식, 제목 매칭 점수식 등의 아이디어와 일부 코드를 가져왔습니다. 원작 저장소에는 LICENSE 파일이 없으므로(2026-10 기준) 별도 라이선스를 부여하지 않으며, 원작자의 요청이 있으면 수정/삭제합니다.
- 데이터: [starsavior-guide.github.io](https://starsavior-guide.github.io/data/) 의 공개 DB (`journey/<언어>/journey.json`, `arcanas/arcanas.json`). 데이터의 권리는 제공자/게임사에 있으며, 이 저장소는 여정 DB 본체를 포함하지 않고 실행 시 내려받습니다(구원자 추천 아르카나는 가이드 사이트에서 추출한 슬림 JSON `data/savior_reco.json` 을 포함)(테스트용 축소 샘플만 `tests/fixtures`에 포함). 단, 새 DB에 없는 정보는 옛 DB(star-savior-arcana-db, 2026-10-15 백업)에서 추출해 코드에 내장했습니다: 여정 버프 효과 설명 `jh_buffs.py`, 턴별 일정 `jh_schedule.py`.
- OCR: [RapidOCR](https://github.com/RapidAI/RapidOCR) + [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) PP-OCRv4 한국어 인식 모델(`models/`, Apache-2.0).
- PyQt5(GPL v3/상용), onnxruntime(MIT), OpenCV(Apache-2.0), mss(MIT), requests(Apache-2.0).

## 데이터 위치 / 업데이트
- 설정·캐시·로그는 모두 **프로그램 폴더 안**(`data\`, `log\`)에 저장됩니다(휴대형). 폴더를 지워도 프로그램이 다시 만들며, 용량이 부담되면 `data\cache`나 `log\`를 지워도 됩니다. 프로그램 폴더에 쓸 수 없는 위치면 `%APPDATA%\JourneyHelperLite`로 대신 저장합니다.
- 새 버전은 기존 폴더에 **덮어쓰기**하면 설정이 유지됩니다(`data\`, `log\`는 압축 파일에 들어 있지 않습니다). 버전 표기는 `1.0.01.YYMMDD`(마지막은 빌드 날짜)입니다.
- 문제가 생기면 설정창의 `고급·진단`에서 **진단 정보 복사**를 눌러 붙여넣어 제보해 주세요.
