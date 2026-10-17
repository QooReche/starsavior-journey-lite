# -*- coding: utf-8 -*-
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402

pytest.importorskip("PyQt5")
from PyQt5.QtWidgets import QApplication  # noqa: E402

import jh_settings_ui as ui  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeHost:
    def __init__(self):
        self.settings = dict(jhm.DEFAULT_SETTINGS)
        self.settings["hidden_difficulties"] = []
        self.lang = "ko"
        self._hidden = False
        self.db_event_cache = [1, 2, 3]
        self.store = type("S", (), {"meta": {"files": {"a": {}}, "last_check": 0}, "base_url": "https://x"})()
        self.saved = 0
        self.scale_calls = []
        self.lang_calls = []

    def set_value(self, k, v):
        self.settings[k] = v
        self.saved += 1

    def set_scale(self, v):
        self.scale_calls.append(v)
        self.set_value("tooltip_font_scale", v)

    def reset_badge_pos(self): pass
    def pending_update(self): return None
    def refresh_db(self): pass
    def manual_reset(self): pass
    def quit_app(self): pass

    def _toggle_hidden(self):
        self._hidden = not self._hidden

    def on_language_selected(self, v):
        self.lang_calls.append(v)       # 재시작 확인에서 '아니오' 한 것과 같다(lang 불변)


def test_toggles_apply_immediately(app):
    h = FakeHost()
    d = ui.SettingsDialog(h, jhm)
    d.chk_low.setChecked(True)
    d.chk_sched.setChecked(False)
    d.chk_cap.setChecked(True)
    assert h.settings["low_spec_mode"] is True and h.settings["show_schedule"] is False
    assert h.settings["show_capture_debug"] is True
    d.chk_diff["hard"].setChecked(True)
    assert h.settings["hidden_difficulties"] == ["hard"]
    d.cb_start.setCurrentIndex(d.cb_start.findData("expanded"))
    assert h.settings["startup_state"] == "expanded"
    d.cb_scale.setCurrentIndex(d.cb_scale.findData(1.2))
    assert h.scale_calls == [1.2]


def test_refresh_does_not_write(app):
    h = FakeHost()
    h.settings["low_spec_mode"] = True
    d = ui.SettingsDialog(h, jhm)
    assert d.chk_low.isChecked() and h.saved == 0
    h.settings["low_spec_mode"] = False
    d.refresh()
    assert not d.chk_low.isChecked() and h.saved == 0


def test_language_declined_reverts(app):
    h = FakeHost()
    d = ui.SettingsDialog(h, jhm)
    d.cb_lang.setCurrentIndex(d.cb_lang.findData("en"))
    d.cb_lang.activated.emit(d.cb_lang.currentIndex())
    assert h.lang_calls == ["en"] and d.cb_lang.currentData() == "ko"


def test_scrub_paths_masks_user(monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: r"C:\Users\홍길동abc")
    s = ui.scrub_paths(r"open C:\Users\홍길동abc\AppData\x and c:/users/Other/y and 홍길동abc")
    assert "홍길동abc" not in s and "Other" not in s
    assert r"C:\Users\<user>\AppData" in s


def test_diagnostics_masks_home_and_has_fields(app, tmp_path, monkeypatch):
    log = tmp_path / "log.txt"
    home = os.path.expanduser("~")
    log.write_text("\n".join(f"line{i} {home}\\x" for i in range(60)), encoding="utf-8")
    monkeypatch.setenv("JH_LOG_PATH", str(log))
    text = ui.build_diagnostics(FakeHost(), jhm)
    assert jhm.APP_VERSION in text and "events=3" in text and "savior reco:" in text
    assert "line59" in text and "line19" not in text and "line20" in text      # 마지막 40줄
    base = os.path.basename(home)
    assert base.lower() not in text.lower()


def test_pause_button_and_single_instance(app):
    h = FakeHost()
    d = ui.SettingsDialog(h, jhm)
    d.btn_pause.click()
    assert h._hidden is True


def test_open_settings_single_instance(app, tmp_path, monkeypatch):
    monkeypatch.setenv("JH_APP_DIR", str(tmp_path))
    mgr = FakeHost()                # 전체 초기화(스레드·OCR) 없이 open_settings 만 빌려 쓴다
    jhm.MouseHoverManager.open_settings(mgr)
    first = mgr._settings_dlg
    jhm.MouseHoverManager.open_settings(mgr)
    assert mgr._settings_dlg is first and first.isVisible()
    assert first.windowFlags() & jhm.Qt.WindowStaysOnTopHint
    first.close()
    assert not first.isVisible()
    jhm.MouseHoverManager.open_settings(mgr)
    assert mgr._settings_dlg is first and first.isVisible()
    first.close()


def test_menus_have_settings_entry():
    # build_main_menu 는 self.status/_hidden 만 필요하다
    app = QApplication.instance() or QApplication([])
    mgr = FakeHost()
    mgr.status = type("S", (), {"collapsed": True})()
    mgr._toggle_collapsed = mgr.manual_reset = mgr.refresh_db = mgr.open_settings = lambda: None
    menu = jhm.MouseHoverManager.build_main_menu(mgr)       # 메뉴(액션 소유자)를 살려 둬야 한다
    texts = [a.text() for a in menu.actions()]
    assert "⚙️ 설정 열기" in texts and texts[-1] == "🛑 프로그램 종료"
    assert app is not None


def test_clear_cache_and_log_buttons(app, monkeypatch):
    import jh_paths
    monkeypatch.delenv("JH_LOG_PATH", raising=False)
    jhm.save_settings(jhm.load_settings())
    cdir = jh_paths.cache_dir()
    open(os.path.join(cdir, "meta.json"), "w").write("{}")
    jhm.log("log line")
    d = ui.SettingsDialog(FakeHost(), jhm)
    assert d.lbl_usage.text()
    monkeypatch.setattr(d, "_confirm", lambda text: False)
    d._clear_cache()
    d._clear_logs()
    assert os.path.exists(os.path.join(cdir, "meta.json")) and os.path.exists(jh_paths.log_path())
    monkeypatch.setattr(d, "_confirm", lambda text: True)
    d._clear_cache()
    assert os.listdir(cdir) == [] and os.path.isdir(cdir)
    assert os.path.exists(os.path.join(jhm.get_settings_dir(), "settings.json"))
    d._clear_logs()
    jhm.log("again")
    assert "again" in open(jh_paths.log_path(), encoding="utf-8").read()


def test_diagnostics_has_data_location(app, monkeypatch):
    monkeypatch.delenv("JH_LOG_PATH", raising=False)
    text = ui.build_diagnostics(FakeHost(), jhm)
    assert "data location: portable" in text and "cache=" in text


def test_open_log_dir_uses_new_log_dir(app, monkeypatch):
    import jh_paths
    opened = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    ui.SettingsDialog(FakeHost(), jhm)._open_log_dir()
    assert opened == [jh_paths.log_dir()] and opened[0].endswith("log")


# ---- 새 버전 알림 UI ----
def _upd_host(latest="9.9.99.999999", dismissed=""):
    import jh_update
    h = FakeHost()
    h.settings["dismissed_update_version"] = dismissed
    h.update_checker = type("C", (), {"latest": latest, "summary": lambda self: "x"})()
    h.pending_update = lambda: jh_update.visible_update(h.update_checker.latest, jhm.APP_VERSION, h.settings["dismissed_update_version"])
    h.opened = []
    h.open_release_page = lambda v=None: h.opened.append(v)

    def dismiss():
        h.settings["dismissed_update_version"] = h.update_checker.latest
    h.dismiss_update = dismiss
    h.pending_cb = None
    h.check_update_now = lambda cb: setattr(h, "pending_cb", cb)
    return h


def test_update_banner_and_dismiss(app):
    h = _upd_host()
    d = ui.SettingsDialog(h, jhm)
    assert not d.banner.isHidden()
    assert "9.9.99.999999" in d.banner_text.text()
    d._dismiss_update()
    assert d.banner.isHidden() and h.settings["dismissed_update_version"] == "9.9.99.999999"
    assert ui.SettingsDialog(_upd_host(dismissed="9.9.99.999999"), jhm).banner.isHidden()
    assert ui.SettingsDialog(_upd_host(latest=jhm.APP_VERSION), jhm).banner.isHidden()


def test_check_updates_toggle_saves(app):
    h = _upd_host()
    d = ui.SettingsDialog(h, jhm)
    assert d.chk_upd.isChecked()
    d.chk_upd.setChecked(False)
    assert h.settings["check_updates"] is False


def test_manual_check_results(app):
    import jh_update
    R = jh_update.UpdateResult
    h = _upd_host(latest=None)
    d = ui.SettingsDialog(h, jhm)
    d._check_now()
    assert not d.btn_check.isEnabled() and h.pending_cb is not None
    d._on_check_done(R("updated", jhm.APP_VERSION, "", 0))
    assert d.btn_check.isEnabled() and "최신 버전입니다" in d.lbl_check.text() and d.btn_get.isHidden()
    d._on_check_done(R("updated", "9.9.99.999999", "", 0))
    assert "새 버전 9.9.99.999999" in d.lbl_check.text() and not d.btn_get.isHidden()
    d._on_check_done(R("error", None, "x", 0))
    assert "확인하지 못했어요" in d.lbl_check.text() and d.btn_get.isHidden()
    d._on_check_done(R("cooldown", None, "x", 301))
    assert "6분 뒤에" in d.lbl_check.text()


def test_manual_check_shows_dismissed_version(app):
    import jh_update
    h = _upd_host(latest="9.9.99.999999", dismissed="9.9.99.999999")
    d = ui.SettingsDialog(h, jhm)
    assert d.banner.isHidden()
    d._on_check_done(jh_update.UpdateResult("updated", "9.9.99.999999", "", 0))
    assert not d.btn_get.isHidden() and "새 버전" in d.lbl_check.text()
