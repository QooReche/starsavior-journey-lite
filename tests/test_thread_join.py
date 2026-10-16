# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402


class T:
    def __init__(self, running, finishes):
        self.running, self.finishes, self.waited = running, finishes, None

    def isRunning(self):
        return self.running

    def wait(self, ms):
        self.waited = ms
        if self.finishes:
            self.running = False
        return self.finishes


def test_join_returns_only_stuck_threads():
    done, ok, stuck = T(False, True), T(True, True), T(True, False)
    assert jhm.join_threads([done, ok, stuck], 1000) == [stuck]
    assert done.waited is None and ok.waited is not None


def test_join_empty():
    assert jhm.join_threads([], 100) == []
