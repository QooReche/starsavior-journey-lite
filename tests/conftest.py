import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 테스트가 실제 앱 폴더의 log\log.txt (또는 %APPDATA%\JourneyHelperLite\log.txt)에 기록하지 않게 한다 (게임 로그 오염 방지)
os.environ.setdefault("JH_LOG_PATH", os.path.join(tempfile.gettempdir(), "jh_test_log.txt"))


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path_factory, monkeypatch):
    """모든 테스트는 임시 앱 폴더(JH_APP_DIR)를 쓴다: 실제 data 폴더나 %APPDATA% 데이터를 건드리지 않는다."""
    import jh_paths
    monkeypatch.setenv("JH_APP_DIR", str(tmp_path_factory.mktemp("app")))
    monkeypatch.setenv("APPDATA", str(tmp_path_factory.mktemp("appdata")))      # 구 위치(마이그레이션 원본)도 임시
    jh_paths.reset_for_tests()
    yield
    jh_paths.reset_for_tests()
