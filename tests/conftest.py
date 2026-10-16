import os
import tempfile

# 테스트가 실제 %APPDATA%\JourneyHelperLite\log.txt 에 기록하지 않게 한다 (게임 로그 오염 방지)
os.environ.setdefault("JH_LOG_PATH", os.path.join(tempfile.gettempdir(), "jh_test_log.txt"))
