from __future__ import annotations

import pytest

from core.aegis.model_guard import assert_safe_model_dir
from core.aegis.skill_guard import MAX_SKILL_BYTES, read_skill_text


def test_model_guard_rejects_pickle_weights(tmp_path):
    (tmp_path / "pytorch_model.bin").write_bytes(b"not-a-real-checkpoint")

    with pytest.raises(ValueError, match="pickle"):
        assert_safe_model_dir(tmp_path)


def test_model_guard_accepts_safetensors(tmp_path):
    (tmp_path / "model.safetensors").write_bytes(b"safe")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    assert_safe_model_dir(tmp_path)


def test_skill_guard_reads_text_inside_root(tmp_path):
    skill = tmp_path / "Skill 1"
    skill.mkdir()
    target = skill / "SKILL.md"
    target.write_text("# Skill\n", encoding="utf-8")

    assert read_skill_text(target, tmp_path).replace("\r\n", "\n") == "# Skill\n"


def test_skill_guard_rejects_escape_and_binary(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    binary = root / "binary.md"
    binary.write_bytes(b"abc\x00def")

    with pytest.raises(ValueError, match="escapes"):
        read_skill_text(outside, root)
    with pytest.raises(ValueError, match="binary"):
        read_skill_text(binary, root)


def test_skill_guard_rejects_oversized_file(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    target = root / "large.md"
    target.write_bytes(b"a" * (MAX_SKILL_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds"):
        read_skill_text(target, root)
