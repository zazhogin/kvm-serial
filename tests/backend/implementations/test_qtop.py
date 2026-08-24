"""Regression tests for Qt keyboard-to-HID translation."""

from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeyEvent

from kvm_serial.backend.implementations import qtop


def _event(key, text="", event_type=QKeyEvent.Type.KeyPress):
    event = MagicMock(spec=QKeyEvent)
    event.key.return_value = key
    event.text.return_value = text
    event.type.return_value = event_type
    return event


@pytest.fixture
def op():
    # Avoid constructing BaseOp: these tests exercise translation only and do
    # not need a live DataCommManager/serial connection.
    instance = qtop.QtOp.__new__(qtop.QtOp)
    instance.layout = "en_US"
    instance.modifier_map = {}
    instance.hid_serial_out = MagicMock()
    return instance


def _modifier_key_for_hid_bit(bit):
    return next(key for key, value in qtop.MODIFIER_TO_VALUE.items() if value == bit)


@pytest.mark.parametrize("local_text", ["C", "\x03", "c"])
def test_ctrl_c_uses_physical_letter_without_adding_shift(op, local_text):
    """macOS shortcut text must not turn Ctrl+C into Ctrl+Shift+C or no key."""

    ctrl_key = _modifier_key_for_hid_bit(0x01)
    op.parse_key(_event(ctrl_key))
    op.parse_key(_event(Qt.Key.Key_C, local_text))

    op.hid_serial_out.send_scancode.assert_called_with(
        bytes([0x01, 0x00, 0x06, 0x00, 0x00, 0x00, 0x00, 0x00])
    )


def test_shift_c_still_produces_uppercase_c(op):
    shift_key = _modifier_key_for_hid_bit(0x02)
    op.parse_key(_event(shift_key))
    op.parse_key(_event(Qt.Key.Key_C, "C"))

    op.hid_serial_out.send_scancode.assert_called_with(
        bytes([0x02, 0x00, 0x06, 0x00, 0x00, 0x00, 0x00, 0x00])
    )


def test_ctrl_left_bracket_uses_physical_punctuation_key(op):
    ctrl_key = _modifier_key_for_hid_bit(0x01)
    op.parse_key(_event(ctrl_key))
    # Some platforms expose Ctrl+[ as the Escape control character in text().
    op.parse_key(_event(Qt.Key.Key_BracketLeft, "\x1b"))

    op.hid_serial_out.send_scancode.assert_called_with(
        bytes([0x01, 0x00, 0x2F, 0x00, 0x00, 0x00, 0x00, 0x00])
    )


def test_release_all_clears_stale_modifiers(op):
    ctrl_key = _modifier_key_for_hid_bit(0x01)
    op.parse_key(_event(ctrl_key))

    op.release_all()

    assert op.modifier_map == {}
    op.hid_serial_out.send_scancode.assert_called_with(b"\x00" * 8)
