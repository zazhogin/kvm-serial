from pathlib import Path
import sys
from unittest.mock import patch

from kvm_serial.utils import settings


def test_source_run_keeps_legacy_project_local_path():
    with patch.object(sys, "frozen", False, create=True):
        assert settings.get_settings_path() == ".kvm_settings.ini"


def test_frozen_macos_app_uses_application_support():
    with (
        patch.object(sys, "frozen", True, create=True),
        patch.object(sys, "platform", "darwin"),
        patch.object(Path, "home", return_value=Path("/Users/tester")),
    ):
        assert settings.get_settings_path() == str(
            Path("/Users/tester/Library/Application Support/KVM Serial/settings.ini")
        )


def test_save_settings_creates_parent_directory(tmp_path):
    settings_path = tmp_path / "nested" / "KVM Serial" / "settings.ini"

    settings.save_settings(str(settings_path), "KVM", {"baud_rate": 57600})

    assert settings_path.exists()
    assert settings.load_settings(str(settings_path), "KVM") == {"baud_rate": "57600"}
