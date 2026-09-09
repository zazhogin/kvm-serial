import configparser
import os
import logging
from pathlib import Path
import sys
from typing import Dict, Any

LEGACY_SETTINGS_FILE = ".kvm_settings.ini"


def get_settings_path() -> str:
    """Return a writable settings path for source and packaged executions.

    Source runs retain the historical project-local INI file. Frozen apps use
    the platform's per-user configuration directory because their working
    directory and application bundle are not writable locations on macOS.
    """
    if not getattr(sys, "frozen", False):
        return LEGACY_SETTINGS_FILE

    if sys.platform == "darwin":
        directory = Path.home() / "Library" / "Application Support" / "KVM Serial"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        directory = Path(appdata) / "KVM Serial" if appdata else Path.home() / "KVM Serial"
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        directory = (
            Path(config_home) / "kvm-serial"
            if config_home
            else Path.home() / ".config" / "kvm-serial"
        )

    return str(directory / "settings.ini")


def load_settings(
    config_file: str, section: str, defaults: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """
    Load settings from an INI file. Returns a dict of settings for the given section.
    If the file or section does not exist, returns defaults (if provided) or empty dict.
    """
    config = configparser.ConfigParser()
    if not os.path.exists(config_file):
        return defaults.copy() if defaults is not None else {}
    config.read(config_file, encoding="utf-8")
    if section not in config:
        return defaults.copy() if defaults is not None else {}
    settings = dict(config[section])
    # Overlay defaults for missing keys
    if defaults is not None:
        for k, v in defaults.items():
            settings.setdefault(k, v)
    logging.info(f"Settings loaded from {config_file} [{section}]")
    return settings


def save_settings(config_file: str, section: str, settings: Dict[str, Any]) -> None:
    """
    Save settings to an INI file under the given section.
    """
    config = configparser.ConfigParser()
    if os.path.exists(config_file):
        config.read(config_file, encoding="utf-8")
    config[section] = {k: str(v) for k, v in settings.items()}

    parent_directory = os.path.dirname(os.path.abspath(config_file))
    os.makedirs(parent_directory, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        config.write(f)
    logging.info(f"Settings saved to {config_file} [{section}]")
