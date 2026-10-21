from __future__ import annotations

from core.aegis.time_guard import ClockGuard, detect_preload, parse_server_time


def test_parse_server_time_iso_and_epoch():
    assert parse_server_time(1000) == 1000.0
    assert parse_server_time("1970-01-01T00:16:40Z") == 1000.0
    assert parse_server_time("1970-01-01T00:16:40+00:00") == 1000.0


def test_clock_rollback_is_detected():
    guard = ClockGuard(tolerance_seconds=300)
    guard.record_server_time(1000, now=1000)

    assert guard.check(now=800).ok is True
    result = guard.check(now=600)
    assert result.ok is False
    assert result.reason == "clock_rollback"
    assert result.rollback_seconds == 400


def test_lease_remaining_is_clamped():
    guard = ClockGuard(tolerance_seconds=300)
    guard.record_server_time(1000, now=1000)

    assert guard.lease_remaining(1100, now=1000) == 100
    assert guard.lease_remaining(900, now=1000) == 0


def test_preload_detection(monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "/usr/lib/libfaketime.so.1")
    result = detect_preload()
    assert result["detected"] is True
    assert "LD_PRELOAD" in result["reasons"]
