from __future__ import annotations

from pathlib import Path

from scripts import agent_service


def test_linux_service_runs_agent_boot_after_repair(monkeypatch):
    monkeypatch.setattr(agent_service.platform, "system", lambda: "Linux")
    spec = agent_service.definition(system=False)

    assert Path(spec["path"]).name == "x5agent.service"
    assert ".config" in spec["path"] and "systemd" in spec["path"]
    assert "ExecStartPre=" in spec["content"]
    assert "agent_repair.py" in spec["content"]
    assert "ExecStart=" in spec["content"]
    assert "agent_boot.py" in spec["content"]
    assert "Restart=always" in spec["content"]
    assert any("enable --now" in line for line in spec["enable"])


def test_macos_service_keeps_agent_boot_alive(monkeypatch, capsys):
    monkeypatch.setattr(agent_service.platform, "system", lambda: "Darwin")
    spec = agent_service.definition()

    assert Path(spec["path"]).name == "com.x5agent.agent.plist"
    assert "LaunchAgents" in spec["path"]
    assert "com.x5agent.agent" in spec["content"]
    assert "KeepAlive" in spec["content"]
    assert agent_service.main(["--print"]) == 0
    assert "com.x5agent.agent" in capsys.readouterr().out


def test_windows_service_uses_scheduled_task(monkeypatch, capsys):
    monkeypatch.setattr(agent_service.platform, "system", lambda: "Windows")
    spec = agent_service.definition()

    assert spec["scope"] == "machine"
    command = spec["enable"][0]
    assert "schtasks /Create" in command
    assert "agent_boot.py" in command
    assert "ONSTART" in command
    assert agent_service.main(["--print"]) == 0
    assert "schtasks /Create" in capsys.readouterr().out
