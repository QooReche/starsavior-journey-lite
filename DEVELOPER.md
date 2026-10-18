# 개발자용 문서 (여정 내비게이터 / JourneyNavigator)

일반 사용자용 안내는 `README.md`. 이 문서는 소스/빌드/데이터/구조를 다룹니다.

스타세이비어 여정 이벤트·아르카나 편성 화면을 화면 인식(OCR)으로 읽어 오버레이 툴팁으로 보여 주는 도구입니다.
원작 [p1021/journeyhelper](https://github.com/p1021/journeyhelper)를 가볍게 다시 만든 것으로, 계산기류 없이 "화면 인식 -> 정보 표시"만 합니다.

## 원작 대비

| | 원작 | Lite |
|---|---|---|
| OCR | easyocr + torch (수 GB) | rapidocr-onnxruntime (CPU, 한국어 rec 모델 24MB) |
| 데이터 | star-savior-arcana-db (종료 예정) | starsavior-guide.github.io + V2(starsavior-journey-choice.pages.dev), 로컬 캐시·조건부 갱신·내장 시드 |
| 카드 그림 인식 | 있음(imagehash) | 있음(자체 Lab/HSV 특징 비교, `jh_cards.py`) |
| 설정 | 큰 설정창 | 가벼운 설정창(탭 3개) + 상단바 메뉴 |
| 추가 | | 구원자 추천 아르카나, 일정 미리보기(베타), '훈련의 방향성' 특수 잠재력 안내, 새 버전 알림 |
| 크기 | 약 1.3GB | 약 240MB(zip 약 106MB) |
| 이름 | JourneyHelper | 여정 내비게이터 / JourneyNavigator (2026-10-18 변경, 이전 이름 폴더의 설정은 이어받음) |

## 소스로 실행

Windows 10/11, Python 3.10~3.12 (개발/검증: 3.11).
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python journey_helper.py
```
- 한글이 들어간 경로에서 Qt5 `QApplication`이 멈추는 환경이 있어, 시작 시 Qt 플러그인을 ASCII 경로로 복사해 우회합니다(앱 폴더의 `data` 경로가 ASCII면 `data\qt_plugins`, 아니면 `%LOCALAPPDATA%\JourneyNavigator\qt_plugins`). 사용자가 지울 때 이 폴더도 함께 지워야 하므로 README에 안내합니다. 개발/빌드는 ASCII 경로를 권장합니다.
- 모든 이미지 입출력은 한글 경로에서도 되도록 `cv2.imencode`/`tofile` 방식입니다(`tests/test_korean_path.py`가 한글 폴더로 검증).

## 테스트

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python journey_helper.py --selftest [--no-update] [--lang en] [--out result.txt]
```
- 테스트는 실제 `log.txt`를 건드리지 않습니다(`tests/conftest.py`).
- 카드 식별/일정/툴팁 회귀 픽스처는 `tests/fixtures/`(스크린샷 크롭, 참조 카드 PNG)에 있습니다.
- `--selftest`: DB 로드 -> 캐시 구성 -> 샘플 툴팁 HTML 출력 -> OCR 모델 로딩 확인 후 종료.

## 빌드 / 배포

빌드는 **ASCII 경로**(`C:\build\jh`)에서 합니다. 한 번에:
```powershell
powershell -File tools\setup_build_env.ps1                       # 최초 1회: 소스 사본 + venv + .pth + 테스트
powershell -File tools\build_release.ps1 -Deploy -UpdateHandoff  # 이후: 동기화 -> 날짜 스탬프 -> pytest -> PyInstaller -> zip/sha256 -> (배포·재실행)
```
- `build_release.ps1`은 PyInstaller 직후 `tools\collect_licenses.py`로 `LICENSE`, `README.md`, `THIRD_PARTY_NOTICES.txt`, `licenses\`(번들 라이브러리 라이선스 원문)를 dist에 채우고(GPL/Apache/MIT가 배포물과 함께 사본 동봉을 요구), `make_release_zip.ps1`이 이 파일들이 zip에 있는지 검사합니다. 소스 동기화는 `/PURGE`라 src에서 지운 파일이 빌드에 남지 않습니다. pytest가 실패하면 중단하고, 커밋/푸시는 하지 않습니다. 실행해 본 적 없는 `dist`만 zip으로 만듭니다(`make_release_zip.ps1`; `data\`·`log\`가 있으면 거부).
- 빌드 venv의 site-packages에 `import onnxruntime` 한 줄짜리 `zz_ort_first.pth`가 **반드시** 있어야 합니다. 없으면 PyInstaller 분석이 PyQt5를 먼저 불러 onnxruntime에서 access violation으로 죽습니다(앱 코드도 같은 이유로 onnxruntime을 먼저 import).
- spec이 PyQt5가 동봉한 오래된 VC++ 런타임(14.26)을 번들에서 제외합니다(포함되면 onnxruntime이 시작 시 죽음).
- 버전 표기는 `BASE.YYMMDD`(`jh_version.py`). 빌드 직전 `tools\stamp_version.py`가 날짜를 갱신합니다.
- 릴리즈는 zip 해시가 바뀔 때마다 VirusTotal 검사를 다시 해야 의미가 있습니다. 릴리즈 본문은 패치노트 형식으로 변경점을 간략히 적습니다.

## 구조 (주요 모듈)

| 파일 | 역할 |
|---|---|
| `journey_helper.py` | 메인(PyQt5 오버레이, 화면 감시 스레드, 호버 판정, 상단바/트레이, 설정 연동) |
| `jh_db.py` | 가이드 DB 정규화·이벤트 캐시, 제목 매칭(자모 편집거리), 툴팁 HTML, 아이템 사용처 연결 |
| `jh_v2.py` | 새 DB V2 보충(선택지 구성이 다르면 V2 우선, 아이템/버프 설명, 인비저블 아이템 숨김) |
| `jh_cards.py` | 아르카나 카드 썸네일 식별(2단계: 좁은 탐색 -> 실패 시 위치·배율 넓은 탐색, 엄격 기준) |
| `jh_savior.py` | 구원자 식별(이름+부제 OCR)·추천 아르카나 툴팁(디펜더 2세트 포함) |
| `jh_relic.py` | '훈련의 방향성' 선택지별 특수 잠재력(AX~XX) 안내 + 구원자별 추천 |
| `jh_equip.py` | 구원자 '장비' 화면: 화면 판별(머리글 '구원자' + 제목 '장비' + 버튼), 구원자 식별 상태기계(`EquipTracker`), PVE 추천 장비 툴팁. 좌표는 `EQUIP_REGIONS`(스크린샷 2장으로 보정, `tests/test_equip.py`) |
| `jh_common.py` | 보상 줄 정규식(결과 태그·택1·난이도)과 툴팁 HTML 조각(`span`, 색 상수): `jh_db`/`jh_schedule`/`jh_v2`/`jh_relic` 공용 |
| `jh_schedule.py` | 턴별 일정(내장 데이터), 고정 이벤트 주요 보상 요약, 랜덤 이벤트 증감 요약 |
| `jh_buffs.py` | 새 DB에 없는 여정 버프 설명 보충 |
| `jh_net.py` | 네트워크 제한(backoff, 서킷브레이커, 시간당 상한) |
| `jh_update.py` | 새 버전 확인(`releases/latest` 302 리다이렉트 사용) |
| `jh_paths.py` | 휴대형 데이터 경로(`data\`, `log\`), 폴백 `%APPDATA%` |
| `jh_settings_ui.py` | 설정창(일반 / 게임 표시 / 고급·진단) |
| `tools/` | 빌드 스크립트, 시드/추천 데이터 추출기(`make_seed.py`, `extract_savior_reco.py`), 데이터북 대조 점검(`check_databook.py <jev.json>`: 내장 일정·유물 경로가 데이터북과 어긋나는 곳 보고) |
| `assets/` | 시드 DB(`seed/`), 구원자 추천 슬림 JSON(`savior_reco.json`) |

## 데이터 출처와 갱신 정책

- 이벤트: [starsavior-guide.github.io/data](https://starsavior-guide.github.io/data/) `journey/<언어>/journey.json`, `arcanas/arcanas.json` + V2 `starsavior-journey-choice.pages.dev/json/`. 앱은 실행 중 이 서버에 **조건부 GET(304)**으로만 요청하고, 연속 실패 시 backoff(10분→30분→2시간→6시간, 재시작해도 유지), DB/V2 자동 갱신 24시간당 8회·이미지 시간당 200장 상한, 카드 이미지 서킷브레이커를 둡니다. 응답이 줄었거나 구조가 바뀌면 기존 캐시를 유지합니다.
- 오프라인/실패 대비: 한국어 DB 시드(`assets/seed`, 생성일은 `SEED_INFO.txt`)와 구원자 추천 JSON을 내장합니다.
- 새 DB에 없는 정보는 옛 DB(star-savior-arcana-db, 2026-10-15 백업)에서 추출해 코드에 내장했습니다: 여정 버프 설명(`jh_buffs.py`), 턴별 일정(`jh_schedule.py`).
- 구원자 추천은 가이드 사이트 JS(`app-core.js`, `app.js`)의 규칙을 `tools/extract_savior_reco.py`로 포팅해 만든 슬림 JSON입니다(`tools/site_logic_check.js`로 사이트 JS를 직접 실행해 교차 검증). 사이트가 갱신되면 사본을 새로 받아 재추출·재빌드합니다.
- '훈련의 방향성' 선택지→유물 연결은 DB에 없어 아카라이브 정리글과 데이터북(starsavior-databook.pages.dev)을 보고 `jh_relic.ROUTES`에 직접 적었습니다(선택지 이름이 정확히 같을 때만 표시, DB가 바뀌면 `tests/test_relic.py`가 알려 줍니다).
- 일정 일부는 데이터북 기준으로 보정했습니다(모범의 선물, 비치발리볼 대회/온천 축제 시기 등). 데이터북은 재사용 허락 안내가 확인되지 않아 번들·자동 수신하지 않습니다(차후 서브 DB 후보).

## 동작 메모

- 화면 비율 기반 영역(`REGION_FRACTIONS`)에 의존하므로 게임 UI가 개편되면 비율을 고쳐야 합니다. 카드 위치는 창 비율에 따라 밀릴 수 있어 식별 2단계에서 넓게 탐색합니다(기준: 1616x939).
- `화면의 '여정 이벤트 / 훈련 이벤트 / 아르카나 이벤트' 라벨`을 먼저 확인한 경우에만 제목을 읽습니다(장비/스탯 화면 오인식 방지).
- 오버레이는 기본 `WDA_EXCLUDEFROMCAPTURE`라 스크린샷에 안 찍힙니다(설정에서 방송·녹화에 표시 가능, 켜면 OCR에 영향 줄 수 있음).
- 성공/실패 확률은 어느 DB에도 없어 표시하지 않습니다. 평가전 이벤트는 의도적으로 매칭하지 않습니다.
- 영어 모드는 이벤트 툴팁만 준비되어 있고 영어 클라이언트에서는 검증하지 못한 실험 기능입니다.

## 크레딧 / 라이선스

- 코드: GPL-3.0 (PyQt5 GPL 때문에 소스 공개). 원작에서 가져온 부분의 권리는 원작자에게 있고, 원작 저장소에는 LICENSE가 없으므로(2026-10 기준) 원작자의 요청이 있으면 수정/삭제합니다.
- 원작: [p1021/journeyhelper](https://github.com/p1021/journeyhelper) — 화면 영역 비율, 창 탐색, 호버 방식, 제목 매칭 점수식 등의 아이디어와 일부 코드.
- OCR: [RapidOCR](https://github.com/RapidAI/RapidOCR) + PaddleOCR PP-OCRv4 한국어 인식 모델(`models/`, Apache-2.0).
- PyQt5(GPL v3/상용), onnxruntime(MIT), OpenCV(Apache-2.0), mss(MIT), requests(Apache-2.0).
