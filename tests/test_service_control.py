from pathlib import Path
from types import SimpleNamespace

import scripts.service_control as service_control


def test_status_service_reports_not_loaded(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        service_control,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )

    assert service_control.status_service() == 0
    assert "当前未加载" in capsys.readouterr().out


def test_status_service_parses_launchctl_output(monkeypatch, capsys) -> None:
    payload = """
gui/501/com.openai.codex.telegram-bridge = {
    state = running
    pid = 12345
}
""".strip()
    monkeypatch.setattr(
        service_control,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    assert service_control.status_service() == 0
    output = capsys.readouterr().out
    assert "状态：running" in output
    assert "进程：12345" in output


def test_start_service_requires_plist(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(service_control, "plist_path", lambda: tmp_path / "missing.plist")

    assert service_control.start_service() == 1
    assert "未找到 LaunchAgent 配置" in capsys.readouterr().err
