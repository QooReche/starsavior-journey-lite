# -*- coding: utf-8 -*-
"""설정창(1.1.3 스타일): 탭 3개(일반 / 게임 표시 / 고급·진단), 모든 옵션 즉시 적용.

journey_helper 와 순환 import 를 피하려고 SettingsDialog 는 (host, jhm) 를 받는다.
host = MouseHoverManager(설정 보관·적용 담당), jhm = journey_helper 모듈(상수/tr/log 등).
"""
import getpass
import os
import platform
import re
import sys
import time

from PyQt5.QtCore import Qt, QTimer
import jh_paths
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                             QScrollArea, QTabWidget, QVBoxLayout, QWidget)

ACCENT = "#00e5ff"
STYLE = """
QDialog { background-color: #11151f; color: #e6e9ef; font-family: "Malgun Gothic", "Segoe UI"; font-size: 13px; }
QLabel { color: #e6e9ef; background: transparent; }
QLabel#title { color: #00e5ff; font-size: 17px; font-weight: bold; }
QLabel#desc { color: #8b93a3; font-size: 11px; padding-left: 24px; }
QLabel#descPlain { color: #8b93a3; font-size: 11px; }
QLabel#warn { color: #ffb86b; font-size: 11px; padding-left: 24px; }
QLabel#version { color: #aab1bf; }
QTabWidget::pane { border: 1px solid #2a3142; border-radius: 6px; background-color: #151a26; top: -1px; }
QTabBar::tab { background: #1a2030; color: #aab1bf; border: 1px solid #2a3142; border-bottom: none;
               padding: 6px 14px; margin-right: 2px; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { background: #151a26; color: #00e5ff; border-color: #00e5ff; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: #151a26; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #34405a; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCheckBox { color: #e6e9ef; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #4a5570; border-radius: 3px; background: #0d1119; }
QCheckBox::indicator:checked { background: #00b8d4; border-color: #00e5ff; }
QComboBox { background: #0d1119; color: #e6e9ef; border: 1px solid #34405a; border-radius: 4px; padding: 4px 8px; min-width: 110px; }
QComboBox QAbstractItemView { background: #0d1119; color: #e6e9ef; selection-background-color: #00778a; border: 1px solid #34405a; }
QPushButton { background: #1b2232; color: #e6e9ef; border: 1px solid #34405a; border-radius: 4px; padding: 6px 10px; }
QPushButton:hover { background: #242d42; border-color: #00e5ff; }
QPushButton:pressed { background: #0d1119; }
QPushButton#quit { background: #4a1b22; border-color: #8a2d3a; color: #ffd6da; font-weight: bold; }
QPushButton#quit:hover { background: #6a2530; border-color: #ff5c6c; }
QFrame#sep { background: #232a3b; max-height: 1px; border: none; }
"""

HOME_PLACEHOLDER = "<user>"


def scrub_paths(text):
    """사용자 이름/홈 디렉터리를 가린다 (C:\\Users\\<user>)."""
    text = text or ""
    names = set()
    for n in (os.path.basename(os.path.expanduser("~")), os.environ.get("USERNAME"), os.environ.get("USER")):
        if n and len(n) >= 3:
            names.add(n)
    try:
        n = getpass.getuser()
        if n and len(n) >= 3:
            names.add(n)
    except Exception:
        pass
    text = re.sub(r"(?i)([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s\"'<>|:*?]+", r"\1" + HOME_PLACEHOLDER, text)
    for n in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(n), HOME_PLACEHOLDER, text, flags=re.IGNORECASE)
    return text


def _tail_lines(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read()
        return data.decode("utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def build_diagnostics(host, jhm, now=None):
    """클립보드용 진단 텍스트. 민감정보(사용자 이름/경로)는 scrub_paths 로 가린다."""
    L = []
    L.append(f"JourneyHelperLite {jhm.APP_VERSION}")
    L.append(f"time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}")
    L.append(f"OS: {platform.platform()} / Python {platform.python_version()} / frozen={bool(getattr(sys, 'frozen', False))}")
    try:
        rect = jhm.get_game_window_rect()
    except Exception:
        rect = None
    L.append(f"game window: {rect if rect else 'not found'}")
    try:
        scr = []
        for s in QApplication.screens():
            g = s.geometry()
            scr.append(f"{g.width()}x{g.height()}@({g.x()},{g.y()}) dpr={s.devicePixelRatio():.2f}")
        L.append("monitors: " + "; ".join(scr))
    except Exception:
        L.append("monitors: ?")
    L.append("settings:")
    for k in jhm.DEFAULT_SETTINGS:
        L.append(f"  {k} = {host.settings.get(k)}")
    cache = getattr(host, "db_event_cache", None) or []
    meta = getattr(getattr(host, "store", None), "meta", {}) or {}
    last = meta.get("last_check") or 0
    last_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "-"
    seed = getattr(getattr(host, "store", None), "using_seed", False) is True
    L.append(f"DB 출처: {'시드 사용 중 (사이트 데이터 아직 못 받음)' if seed else '캐시'}")
    L.append(f"DB: events={len(cache)} lang={getattr(host, 'lang', '?')} base={getattr(host.store, 'base_url', '?')} "
             f"files={len(meta.get('files', {}))} last_check={last_s}")
    try:
        L.append(host.store.network_summary())
    except Exception as e:
        L.append(f"네트워크: error {type(e).__name__}")
    try:
        reco = jhm.jh_savior.load_reco()
        L.append(f"savior reco: {len(reco)} saviors loaded")
    except Exception as e:
        L.append(f"savior reco: error {type(e).__name__}")
    try:
        L.append(jh_paths.usage_summary())
    except Exception as e:
        L.append(f"data location: error {type(e).__name__}")
    log_path = jh_paths.log_path()
    L.append("log.txt (last 40 lines):")
    L.extend(_tail_lines(log_path, 40))
    return scrub_paths("\n".join(L))


class SettingsDialog(QDialog):
    def __init__(self, host, jhm):
        super().__init__(None)
        self.host, self.jhm = host, jhm
        self.tr_ = jhm.tr
        self._loading = False
        self.setWindowTitle("JourneyHelper " + self.tr_("설정"))
        self.setWindowFlags((self.windowFlags() | Qt.WindowStaysOnTopHint) & ~Qt.WindowContextHelpButtonHint)
        self.setStyleSheet(STYLE)
        self.resize(460, 620)
        self.setMinimumWidth(430)
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._reset_copy_text)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)
        title = QLabel(self.tr_("⚙ 설정"))
        title.setObjectName("title")
        root.addWidget(title)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._scroll(self._build_general), self.tr_("일반"))
        self.tabs.addTab(self._scroll(self._build_display), self.tr_("게임 표시"))
        self.tabs.addTab(self._scroll(self._build_advanced), self.tr_("고급·진단"))

        ver = QLabel(self.tr_("현재 버전: {}").format(jhm.APP_VERSION))
        ver.setObjectName("version")
        root.addWidget(ver)
        row = QHBoxLayout()
        quit_btn = QPushButton("●  " + self.tr_("프로그램 종료"))
        quit_btn.setObjectName("quit")
        quit_btn.clicked.connect(self._quit)
        close_btn = QPushButton(self.tr_("닫기"))
        close_btn.clicked.connect(self.close)
        row.addWidget(quit_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        root.addLayout(row)
        self.refresh()

    # ---- 빌더 보조 ----
    def _scroll(self, builder):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(6)
        builder(lay)
        lay.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(page)
        return sa

    def _section(self, lay, icon, text, color, first=False):
        if not first:
            sep = QFrame()
            sep.setObjectName("sep")
            lay.addSpacing(6)
            lay.addWidget(sep)
            lay.addSpacing(4)
        lab = QLabel(f"{icon}  {text}")
        lab.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 13px;")
        lay.addWidget(lab)

    def _desc(self, lay, text, warn=False, plain=False):
        lab = QLabel(text)
        lab.setObjectName("warn" if warn else ("descPlain" if plain else "desc"))
        lab.setWordWrap(True)
        lay.addWidget(lab)
        return lab

    def _check(self, lay, text, key, desc=None, warn=None):
        cb = QCheckBox(text)
        cb.toggled.connect(lambda v, k=key: None if self._loading else self.host.set_value(k, bool(v)))
        lay.addWidget(cb)
        if desc:
            self._desc(lay, desc)
        if warn:
            self._desc(lay, warn, warn=True)
        return cb

    def _combo_row(self, lay, text):
        row = QHBoxLayout()
        row.addWidget(QLabel(text), 1)
        cb = QComboBox()
        row.addWidget(cb)
        lay.addLayout(row)
        return cb

    def _button(self, lay, text, slot):
        b = QPushButton(text)
        b.clicked.connect(lambda _=False: slot())
        lay.addWidget(b)
        return b

    # ---- 탭: 일반 ----
    def _build_general(self, lay):
        t = self.tr_
        self._section(lay, "🚀", t("시작"), "#ff9f43", first=True)
        self.cb_start = self._combo_row(lay, t("시작할 때 상단바 상태"))
        for label, val in ((t("접힘"), "collapsed"), (t("펼침"), "expanded")):
            self.cb_start.addItem(label, val)
        self.cb_start.currentIndexChanged.connect(
            lambda _i: None if self._loading else self.host.set_value("startup_state", self.cb_start.currentData()))
        self._desc(lay, t("프로그램을 켰을 때 상단바를 접어 둘지 펼칠지 정해요."))

        self._section(lay, "🌐", t("언어"), "#4dabf7")
        self.cb_lang = self._combo_row(lay, t("표시 언어"))
        for label, val in (("한국어", "ko"), ("English", "en")):
            self.cb_lang.addItem(label, val)
        self.cb_lang.activated.connect(self._on_lang)
        self._desc(lay, t("바꾸면 이벤트 데이터를 새 언어로 다시 불러오기 위해 재시작해야 해요."))

        self._section(lay, "📌", t("상단바"), "#ff6b81")
        self.chk_lock = self._check(lay, t("상단바 위치 고정"), "status_badge_locked",
                                    t("끄면 상단바를 드래그해서 옮길 수 있어요."))
        self._button(lay, "↺  " + t("상단바 위치 초기화"), self.host.reset_badge_pos)
        self.btn_pause = self._button(lay, "", self._toggle_pause)

        self._section(lay, "⚡", t("성능"), "#feca57")
        self.chk_low = self._check(lay, t("저사양 모드 (OCR 주기 늘림)"), "low_spec_mode",
                                   t("화면을 읽는 주기를 늘려 CPU 사용량을 줄여요. 반응이 조금 느려질 수 있어요."))

    # ---- 탭: 게임 표시 ----
    def _build_display(self, lay):
        t = self.tr_
        self._section(lay, "💬", t("툴팁"), "#00e5ff", first=True)
        self.cb_scale = self._combo_row(lay, t("왼쪽 툴팁 글자 크기"))
        for label, val in self.jhm.FONT_SCALE_CHOICES:
            self.cb_scale.addItem(t(label), val)
        self.cb_scale.currentIndexChanged.connect(
            lambda _i: None if self._loading else self.host.set_scale(self.cb_scale.currentData()))
        self._desc(lay, t("선택지에 마우스를 올렸을 때 나오는 툴팁의 글자 크기예요."))
        self.chk_info = self._check(lay, t("잠재력/버프 설명 표시"), "show_info",
                                    t("툴팁에 잠재력·버프 효과 설명을 함께 보여줘요."))
        lay.addSpacing(4)
        lay.addWidget(QLabel(t("🎚️ 툴팁에서 숨길 난이도")))
        row = QHBoxLayout()
        self.chk_diff = {}
        for label, val in self.jhm.DIFFICULTY_CHOICES[1:]:
            cb = QCheckBox(t(label))
            cb.toggled.connect(lambda checked, v=val: None if self._loading else self.host.set_value(
                "hidden_difficulties", self.jhm.toggle_hidden_difficulty(self.host.settings.get("hidden_difficulties"), v, checked)))
            self.chk_diff[val] = cb
            row.addWidget(cb)
        row.addStretch(1)
        lay.addLayout(row)
        self._desc(lay, t("체크한 난이도의 수치는 툴팁에서 빼요."))

        self._section(lay, "🧩", t("기능"), "#a29bfe")
        self.chk_sched = self._check(lay, t("📅 일정 미리보기"), "show_schedule",
                                     t("게임 좌측 상단 날짜에 마우스를 올리면 이번 턴 일정을 보여줘요."))
        self.chk_reco = self._check(lay, t("🃏 구원자 추천 아르카나"), "show_savior_reco",
                                    t("아르카나 편성 화면에서 추천·대체 카드를 보여줘요.\n카드 고르기 화면에서는 자동으로 숨겨요."))

    # ---- 탭: 고급·진단 ----
    def _build_advanced(self, lay):
        t = self.tr_
        self._section(lay, "🐛", t("진단"), "#ff7675", first=True)
        self.chk_cap = self._check(lay, t("상단바/툴팁을 스크린샷에 보이게 하기"), "show_capture_debug",
                                   t("스크린샷·녹화에 상단바와 툴팁이 찍혀요."),
                                   warn=t("⚠ 진단 전용이에요. 켜 두면 화면 인식(OCR)에 영향을 줄 수 있어요."))
        self._section(lay, "🛠", t("관리"), "#55efc4")
        self._button(lay, "🔄  " + t("DB 새로고침 (10분 쿨다운)"), self.host.refresh_db)
        self._desc(lay, t("가이드 사이트에서 최신 데이터를 다시 받아요."), plain=True)
        self._button(lay, "🎯  " + t("인식 상태 초기화"), self.host.manual_reset)
        self._desc(lay, t("추적 중인 이벤트·선택 잠금을 비우고 화면을 다시 찾아요."), plain=True)
        self._section(lay, "📋", t("문제 확인"), "#74b9ff")
        self._button(lay, "📂  " + t("로그 폴더 열기"), self._open_log_dir)
        self._desc(lay, jh_paths.display_path("log"), plain=True)
        self.lbl_usage = self._desc(lay, "", plain=True)
        self._button(lay, "🧹  " + t("캐시 지우기"), self._clear_cache)
        self._desc(lay, t("내려받은 데이터·카드 이미지 캐시만 지워요. 설정과 사용자 정의 이벤트는 그대로이고, 필요하면 다시 내려받아요."), plain=True)
        self._button(lay, "🗑  " + t("로그 지우기"), self._clear_logs)
        self._desc(lay, t("log 폴더의 로그 파일을 지워요. 새 로그는 자동으로 다시 만들어져요."), plain=True)
        self.btn_copy = self._button(lay, "", self._copy_diag)
        self._reset_copy_text()
        self._desc(lay, t("버전·설정·게임 창·최근 로그를 클립보드에 복사해요. 사용자 이름과 경로는 가려져요."), plain=True)
        self._update_usage()

    # ---- 동작 ----
    def refresh(self):
        """저장된 설정 값을 위젯에 반영 (시그널 없이)."""
        s = self.host.settings
        self._loading = True
        try:
            self.cb_start.setCurrentIndex(max(0, self.cb_start.findData(s.get("startup_state"))))
            self.cb_lang.setCurrentIndex(max(0, self.cb_lang.findData(self.host.lang)))
            self.cb_scale.setCurrentIndex(max(0, self.cb_scale.findData(self.jhm.snap_font_scale(s.get("tooltip_font_scale")))))
            for cb, key in ((self.chk_lock, "status_badge_locked"), (self.chk_low, "low_spec_mode"), (self.chk_info, "show_info"),
                            (self.chk_sched, "show_schedule"), (self.chk_reco, "show_savior_reco"), (self.chk_cap, "show_capture_debug")):
                cb.setChecked(bool(s.get(key)))
            hidden = set(s.get("hidden_difficulties") or [])
            for val, cb in self.chk_diff.items():
                cb.setChecked(val in hidden)
        finally:
            self._loading = False
        self._update_pause_text()
        self._update_usage()

    def _update_pause_text(self):
        self.btn_pause.setText(self.tr_("▶️ 헬퍼 재개") if self.host._hidden else self.tr_("⏸️ 헬퍼 일시정지"))

    def _toggle_pause(self):
        self.host._toggle_hidden()
        self._update_pause_text()

    def _on_lang(self, _i):
        val = self.cb_lang.currentData()
        self.host.on_language_selected(val)        # 재시작 확인. 거절하면 아무것도 바뀌지 않으므로 콤보를 되돌린다
        self._loading = True
        self.cb_lang.setCurrentIndex(max(0, self.cb_lang.findData(self.host.lang)))
        self._loading = False

    def _confirm(self, text):
        return QMessageBox.question(self, "JourneyHelper", text, QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    def _update_usage(self):
        try:
            self.lbl_usage.setText(self.tr_("데이터 {} / 캐시 {}").format(
                jh_paths.fmt_size(jh_paths.dir_size(jh_paths.data_dir())),
                jh_paths.fmt_size(jh_paths.dir_size(jh_paths.cache_dir()))) + " (" + jh_paths.display_path("data") + ")")
        except Exception:
            pass

    def _clear_cache(self):
        if not self._confirm(self.tr_("캐시를 지울까요?\n내려받은 이벤트 데이터와 카드 이미지만 지워지고, 설정과 사용자 정의 이벤트는 유지돼요.")):
            return False
        ok = jh_paths.clear_cache()
        store = getattr(self.host, "store", None)
        try:
            if store is not None and hasattr(store, "_load_meta"):
                store.meta = store._load_meta()          # 지워진 캐시에 맞춰 갱신 메타도 초기화
        except Exception:
            pass
        self.jhm.log("[캐시 지움] 사용자 요청" + ("" if ok else " (일부 실패)"))
        self._update_usage()
        return ok

    def _clear_logs(self):
        if not self._confirm(self.tr_("로그를 지울까요?")):
            return False
        ok = jh_paths.clear_logs()
        self.jhm.log("[로그 지움] 사용자 요청" + ("" if ok else " (일부 실패)"))
        self._update_usage()
        return ok

    def _open_log_dir(self):
        try:
            os.startfile(jh_paths.log_dir())
        except Exception as e:
            self.jhm.log(f"[로그 폴더 열기 실패] {e}")

    def _reset_copy_text(self):
        self.btn_copy.setText("📋  " + self.tr_("진단 정보 복사"))

    def _copy_diag(self):
        text = build_diagnostics(self.host, self.jhm)
        QApplication.clipboard().setText(text)
        self.btn_copy.setText("✔  " + self.tr_("복사됨"))
        self._copy_timer.start(2000)

    def _quit(self):
        self.hide()
        self.host.quit_app()
