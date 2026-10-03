from __future__ import annotations

from pathlib import Path

from kpip.cli import install, main


def test_layered_cli_uses_public_kpip_install_services() -> None:
    source = Path(install.__file__).read_text()
    assert "from kpip.install.metadata import prepare_editable_source" in source
    assert "def _prepare_editable_source" not in source


def test_layered_cli_uninstall_dispatches_to_kpip_install(monkeypatch, capsys) -> None:
    removed: list[str] = []

    def fake_uninstall(name: str) -> bool:
        removed.append(name)
        return True

    from kpip.install.requirements import RequirementInstaller

    monkeypatch.setattr(
        RequirementInstaller,
        "uninstall",
        lambda self, name: fake_uninstall(name),
    )

    assert main.main(["uninstall", "demo-pkg"]) == 0
    assert removed == ["demo-pkg"]
    assert "Successfully uninstalled demo-pkg" in capsys.readouterr().out
