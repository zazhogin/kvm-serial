# Qt Keyboard input implementation
import sys
import logging
from typing import cast
from kvm_serial.utils import ascii_to_scancode, build_scancode, merge_scancodes
from .baseop import BaseOp

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeyEvent

logger = logging.getLogger(__name__)

# HID modifier bits that turn a key press into a shortcut rather than merely
# changing its printable character. Shift and AltGr are intentionally excluded:
# they participate in local text composition rather than selecting a shortcut.
SHORTCUT_MODIFIER_MASK = 0x01 | 0x04 | 0x08 | 0x10 | 0x80

# Qt modifier keys to HID modifier values
MODIFIER_TO_VALUE = {
    Qt.Key.Key_Control: 0x01,
    Qt.Key.Key_Shift: 0x02,
    Qt.Key.Key_Alt: 0x04,
    Qt.Key.Key_Super_L: 0x08,
    Qt.Key.Key_Meta: 0x08,
    # RControl not implemented in Qt: 0x10
    # RShift not implemented in Qt: 0x20
    Qt.Key.Key_AltGr: 0x40,
    Qt.Key.Key_Super_R: 0x80,
}

# Fix for macOS: swap Control and Meta (Command) keys
if sys.platform == "darwin":
    MODIFIER_TO_VALUE[Qt.Key.Key_Control], MODIFIER_TO_VALUE[Qt.Key.Key_Meta] = (
        MODIFIER_TO_VALUE[Qt.Key.Key_Meta],
        MODIFIER_TO_VALUE[Qt.Key.Key_Control],
    )


def _modifier_map(macos_command_as_ctrl: bool = False) -> dict:
    """Return an instance-local modifier map for the selected shortcut mode.

    Qt reports the physical Command key as Key_Control on macOS.  The module
    default above preserves its physical USB identity as GUI/Windows.  The
    optional shortcut mode maps only that key to USB Control, which makes the
    common macOS Command shortcuts work on a Windows target without AutoHotKey.
    Physical Control (reported as Key_Meta by Qt on macOS) remains Control.
    """
    modifier_map = dict(MODIFIER_TO_VALUE)
    if macos_command_as_ctrl:
        command_keys = (
            (Qt.Key.Key_Control,)
            if sys.platform == "darwin"
            else (Qt.Key.Key_Meta, Qt.Key.Key_Super_L, Qt.Key.Key_Super_R)
        )
        for command_key in command_keys:
            modifier_map[command_key] = 0x01
    return modifier_map


# Qt special keys to USB HID scan codes
# NB: USB HID Scancodes DIFFER from PS/2 scan codes!
KEYS_WITH_CODES = {
    ## Alphanumeric keys not listed - handled by character handling
    # Basic keys
    Qt.Key.Key_Enter: 0x28,
    Qt.Key.Key_Return: 0x28,
    Qt.Key.Key_Escape: 0x29,
    Qt.Key.Key_Backspace: 0x2A,
    Qt.Key.Key_Tab: 0x2B,
    Qt.Key.Key_Space: 0x2C,
    # 0x2D-0x38 are punctuation and symbols, handled by character handling
    # - = [ ] \ # ; ' ` , /
    # except period . for some reason...
    Qt.Key.Key_Period: 0x37,
    # Lock keys and function keys
    Qt.Key.Key_CapsLock: 0x39,
    Qt.Key.Key_F1: 0x3A,
    Qt.Key.Key_F2: 0x3B,
    Qt.Key.Key_F3: 0x3C,
    Qt.Key.Key_F4: 0x3D,
    Qt.Key.Key_F5: 0x3E,
    Qt.Key.Key_F6: 0x3F,
    Qt.Key.Key_F7: 0x40,
    Qt.Key.Key_F8: 0x41,
    Qt.Key.Key_F9: 0x42,
    Qt.Key.Key_F10: 0x43,
    Qt.Key.Key_F11: 0x44,
    Qt.Key.Key_F12: 0x45,
    # System and navigation keys
    Qt.Key.Key_Print: 0x46,
    Qt.Key.Key_SysReq: 0x46,
    Qt.Key.Key_ScrollLock: 0x47,
    Qt.Key.Key_Pause: 0x48,
    Qt.Key.Key_Insert: 0x49,  # 0x1000006
    0x1000058: 0x49,  # Also appears to be "Insert"
    Qt.Key.Key_Home: 0x4A,
    Qt.Key.Key_PageUp: 0x4B,
    Qt.Key.Key_Delete: 0x4C,
    Qt.Key.Key_End: 0x4D,
    Qt.Key.Key_PageDown: 0x4E,
    Qt.Key.Key_Right: 0x4F,
    Qt.Key.Key_Left: 0x50,
    Qt.Key.Key_Down: 0x51,
    Qt.Key.Key_Up: 0x52,
    Qt.Key.Key_NumLock: 0x53,
    # Additional keys
    Qt.Key.Key_Menu: 0x65,
}


# macOS virtual key codes identify physical key positions and therefore do not
# change when the user switches input sources.  Qt exposes the NSEvent keyCode
# through QKeyEvent.nativeVirtualKey().  Mapping those positions directly to
# USB HID usages keeps the GUI usable with Cyrillic and other non-ASCII input
# sources; the target OS remains responsible for interpreting its own layout.
MACOS_VIRTUAL_KEY_TO_HID = {
    0x00: 0x04,  # A
    0x01: 0x16,  # S
    0x02: 0x07,  # D
    0x03: 0x09,  # F
    0x04: 0x0B,  # H
    0x05: 0x0A,  # G
    0x06: 0x1D,  # Z
    0x07: 0x1B,  # X
    0x08: 0x06,  # C
    0x09: 0x19,  # V
    0x0A: 0x64,  # ISO section / non-US backslash
    0x0B: 0x05,  # B
    0x0C: 0x14,  # Q
    0x0D: 0x1A,  # W
    0x0E: 0x08,  # E
    0x0F: 0x15,  # R
    0x10: 0x1C,  # Y
    0x11: 0x17,  # T
    0x12: 0x1E,  # 1
    0x13: 0x1F,  # 2
    0x14: 0x20,  # 3
    0x15: 0x21,  # 4
    0x16: 0x23,  # 6
    0x17: 0x22,  # 5
    0x18: 0x2E,  # =
    0x19: 0x26,  # 9
    0x1A: 0x24,  # 7
    0x1B: 0x2D,  # -
    0x1C: 0x25,  # 8
    0x1D: 0x27,  # 0
    0x1E: 0x30,  # ]
    0x1F: 0x12,  # O
    0x20: 0x18,  # U
    0x21: 0x2F,  # [
    0x22: 0x0C,  # I
    0x23: 0x13,  # P
    0x25: 0x0F,  # L
    0x26: 0x0D,  # J
    0x27: 0x34,  # '
    0x28: 0x0E,  # K
    0x29: 0x33,  # ;
    0x2A: 0x31,  # ANSI backslash
    0x2B: 0x36,  # ,
    0x2C: 0x38,  # /
    0x2D: 0x11,  # N
    0x2E: 0x10,  # M
    0x2F: 0x37,  # .
    0x32: 0x35,  # `
}


class QtOp(BaseOp):
    """
    Qt operation mode: parse Qt QKeyEvents to hid_serial_out
    """

    @property
    def name(self):
        return "qt"

    def __init__(
        self,
        serial_port,
        layout: str = "en_GB",
        macos_command_as_ctrl: bool = False,
    ):
        super().__init__(serial_port, layout=layout)
        self.modifier_map = {}
        self.macos_command_as_ctrl = macos_command_as_ctrl
        self.modifier_to_value = _modifier_map(macos_command_as_ctrl)

    def set_macos_command_as_ctrl(self, enabled: bool) -> None:
        """Change shortcut mode after releasing any remotely-held modifiers."""
        if enabled == self.macos_command_as_ctrl:
            return
        self.release_all()
        self.macos_command_as_ctrl = enabled
        self.modifier_to_value = _modifier_map(enabled)

    def run(self):
        raise Exception("Run not supported for Qt mode. Call parse_key from Qt window")

    def parse_key(self, event: QKeyEvent) -> bool:
        """
        Parse a QKeyEvent and convert it to the appropriate scancode

        Args:
            event: QKeyEvent from Qt key press/release

        Returns:
            bool: True if key was processed successfully
        """
        # Determine if this is a press or release
        if event.type() == QKeyEvent.Type.KeyPress:
            self._on_press(event)
        elif event.type() == QKeyEvent.Type.KeyRelease:
            self._on_release(event)
        else:
            logging.warning(f"Got unknown event of kind {type(event)}. Ignoring.")
            return False

        return True

    def _nonalphanumeric_key_to_scancode(self, qt_key: Qt.Key):
        """
        Converts a non-alphanumeric Qt key to its corresponding scancode representation.

        Args:
            qt_key (int): The Qt key code to convert.
        Returns:
            list: A list of 8 bytes representing the scancode.
        Raises:
            KeyError: If the provided key is not found in MODIFIER_TO_VALUE or KEYS_WITH_CODES.
        """
        scancode = [b for b in b"\x00" * 8]

        if qt_key in self.modifier_to_value:
            value = self.modifier_to_value[int(qt_key)]
            scancode[0] = value
            self.modifier_map[qt_key] = scancode
        else:
            value = KEYS_WITH_CODES[qt_key]
            scancode[2] = value

        return scancode

    @staticmethod
    def _macos_physical_key_to_scancode(event: QKeyEvent):
        """Return the HID usage for a printable macOS physical key, if known."""
        if sys.platform != "darwin":
            return None

        native_key = event.nativeVirtualKey()
        # PyQt returns a plain int for native events.  The type check also keeps
        # synthetic/test events without native metadata on the portable path.
        if not isinstance(native_key, int):
            return None

        hid_usage = MACOS_VIRTUAL_KEY_TO_HID.get(native_key)
        if hid_usage is None:
            return None
        return build_scancode(hid_usage)

    def _on_press(self, event: QKeyEvent):
        """
        Function which runs when a key is pressed down

        Args:
            event (QKeyEvent): Qt key event for the pressed key
        """
        qt_key = cast(Qt.Key, event.key())
        scancode = [b for b in b"\x00" * 8]

        try:
            # On macOS, prefer the physical key position for printable keys.
            # event.text() follows the active input source and may contain
            # Cyrillic, while a USB keyboard report must contain HID usages.
            physical_scancode = self._macos_physical_key_to_scancode(event)
            if physical_scancode is not None:
                scancode = physical_scancode
            else:
                try:
                    scancode = self._nonalphanumeric_key_to_scancode(qt_key)
                except KeyError:
                    # This may be an alphanumeric character instead
                    scan_modifiers = merge_scancodes(self.modifier_map.values())
                    shortcut_active = bool(scan_modifiers[0] & SHORTCUT_MODIFIER_MASK)

                    # QKeyEvent.text() describes the text produced by the complete
                    # local shortcut. On macOS Ctrl+C can therefore arrive as "C"
                    # (which ascii_to_scancode turns into Shift+C) or as ETX (\x03,
                    # which is unmapped). For letter shortcuts use the physical Qt
                    # key and merge Ctrl/Alt/GUI separately below. Apply the same
                    # rule to printable punctuation: for example Ctrl+[ can arrive
                    # as Escape text even though the physical key is '['.
                    if shortcut_active and Qt.Key.Key_Space <= qt_key <= Qt.Key.Key_AsciiTilde:
                        text = chr(int(qt_key))
                        if Qt.Key.Key_A <= qt_key <= Qt.Key.Key_Z:
                            text = text.lower()
                    else:
                        text = event.text()
                    if len(text) == 0:
                        # Backup method as event.text() doesn't return for key combos
                        try:
                            text = chr(qt_key).lower()
                        except ValueError:
                            logger.warning(f"Potentially unhandled key: 0x{qt_key:x}")

                    if text and len(text) == 1:
                        scancode = ascii_to_scancode(text, layout=self.layout)
                    else:
                        # Unmapped key - log and skip
                        logger.warning(f"Unmapped Qt key: {qt_key} (0x{qt_key:x}) [0b{qt_key:b}]")
                        return

            scan_modifiers = merge_scancodes(self.modifier_map.values())
            scancode = merge_scancodes([scan_modifiers, scancode])

        except AttributeError as e:
            logging.error("Key not found: " + str(e))
            return

        # Send scancode over serial
        logging.debug(f"{scancode}\t({', '.join([hex(i) for i in scancode])})\t0x{int(qt_key):x}")
        self.hid_serial_out.send_scancode(bytes(scancode))

    def release_all(self) -> None:
        """Release every key and forget modifiers, for example when focus is lost."""

        self.modifier_map.clear()
        self.hid_serial_out.send_scancode(b"\x00" * 8)

    def _on_release(self, event: QKeyEvent):
        """
        Function which runs when a key is released

        Args:
            event (QKeyEvent): Qt key event for the released key
        """
        qt_key = event.key()

        try:
            self.modifier_map.pop(qt_key)
        except KeyError:
            pass  # It might not be a modifier. Ask forgiveness, not permission

        # Send key release (null scancode) layered with remaining modifiers
        scancode = [b for b in b"\x00" * 8]
        scan_modifiers = merge_scancodes(self.modifier_map.values())
        scancode = merge_scancodes([scan_modifiers, scancode])
        logging.debug(f"{scancode}\t({', '.join([hex(i) for i in scancode])})")
        self.hid_serial_out.send_scancode(bytes(scancode))
