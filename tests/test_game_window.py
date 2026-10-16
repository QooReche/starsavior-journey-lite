# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402


def test_non_game_hosts_rejected():
    assert jhm.is_non_game_host("CabinetWClass", "explorer.exe")        # starsavior-journey-lite 폴더 창
    assert jhm.is_non_game_host("Chrome_WidgetWin_1", "code.exe")
    assert jhm.is_non_game_host("Notepad", "notepad.exe")
    assert jhm.is_non_game_host("SomeClass", "WindowsTerminal.exe")


def test_unknown_game_process_allowed():
    assert not jhm.is_non_game_host("UnityWndClass", "StarSavior.exe")
    assert not jhm.is_non_game_host("", "")


def test_title_keywords_still_required():
    assert jhm._title_is_game("StarSavior") and jhm._title_is_game("스타세이비어")
    assert not jhm._title_is_game("메모장")
