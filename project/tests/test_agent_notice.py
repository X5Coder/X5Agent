from __future__ import annotations

from pathlib import Path

import pytest

from scripts import agent_notice


def test_inject_keeps_shebang_and_verifies(tmp_path):
    target = tmp_path / "tool.py"
    target.write_text("#!/usr/bin/env python\nprint('ok')\n", encoding="utf-8")
    used: set[tuple[int, int]] = set()

    assert agent_notice.inject_file(target, used) is True
    text = target.read_text(encoding="utf-8")

    assert text.startswith("#!/usr/bin/env python\n")
    assert agent_notice.NOTICE_TEXT in text
    assert text.count(agent_notice.NOTICE_TEXT) == 1
    assert agent_notice.verify_files([target]) == []
    assert agent_notice.inject_file(target, used) is False
    assert text.count(agent_notice.NOTICE_TEXT) == 1


def test_inject_preserves_crlf(tmp_path):
    target = tmp_path / "tool.py"
    target.write_bytes(b"print('ok')\r\n")
    used: set[tuple[int, int]] = set()

    assert agent_notice.inject_file(target, used) is True
    data = target.read_bytes()

    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert agent_notice.NOTICE_TEXT.encode("utf-8") in data


def test_inject_refuses_json(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError):
        agent_notice.inject_file(target, set())


def test_each_occurrence_gets_a_unique_pair(tmp_path):
    first = tmp_path / "one.py"
    second = tmp_path / "two.py"
    first.write_text("one = 1\n", encoding="utf-8")
    second.write_text("two = 2\n", encoding="utf-8")
    used: set[tuple[int, int]] = set()

    agent_notice.inject_file(first, used)
    agent_notice.inject_file(second, used)

    assert len(used) == 2


def test_verify_reports_missing_notice(tmp_path):
    target = tmp_path / "plain.py"
    target.write_text("value = 1\n", encoding="utf-8")

    assert agent_notice.verify_files([target]) == [target]

def test_verify_reports_duplicate_pair(tmp_path):
    first = tmp_path / "one.py"
    second = tmp_path / "two.py"
    block = agent_notice._block(".py", 7, 999999, "\n")
    first.write_text(block + "one = 1\n", encoding="utf-8")
    second.write_text(block + "two = 2\n", encoding="utf-8")

    assert agent_notice.verify_files([first, second]) == [second]
