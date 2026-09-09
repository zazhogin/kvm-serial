#!/usr/bin/env python
import os
import sys
import logging
import time
import math
from typing import TYPE_CHECKING, Any, cast, Optional

if TYPE_CHECKING:
    from kvm_serial.backend.manager import DataCommManager

# Allow running as a script directly (python kvm_serial/kvm.py) by ensuring
# the project root is first on sys.path so local `kvm_serial.*` imports resolve.
if __name__ == "__main__" and (__package__ is None or __package__ == ""):
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from serial import Serial, SerialException
from PyQt5.QtCore import Qt, QTimer, QSizeF, QRectF, QEvent, QLocale, pyqtSignal
from PyQt5.QtGui import (
    QIcon,
    QMouseEvent,
    QPixmap,
    QKeyEvent,
    QFocusEvent,
    QWheelEvent,
    QPainter,
)
from PyQt5.QtMultimedia import QCamera, QCameraViewfinderSettings, QVideoFrame
from PyQt5.QtMultimediaWidgets import QGraphicsVideoItem
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QLabel,
    QAction,
    QMenu,
    QStatusBar,
    QMessageBox,
    QFileDialog,
    QGraphicsView,
    QGraphicsScene,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

import kvm_serial.utils.settings as settings_util
from kvm_serial.utils.communication import list_serial_ports
from kvm_serial.utils import scancode_to_ascii, string_to_scancodes
from kvm_serial.backend.video import CameraProperties, enumerate_cameras
from kvm_serial.backend.implementations.qtop import QtOp
from kvm_serial.backend.implementations.mouseop import MouseOp, MouseButton

# A CH9329 absolute-mouse report is 13 UART bytes on the wire. UART 8N1 uses
# 10 bits per byte, so the shortest safe interval is derived from the selected
# baud rate (13.54 ms at 9600). Pointer events remain latest-only, but the first
# event after an idle period can now be sent immediately instead of waiting for
# a periodic 20 ms tick.
MOUSE_ABSOLUTE_REPORT_BITS = 13 * 10
KEYBOARD_REPORT_BITS = 14 * 10
LATENCY_DIAGNOSTICS_INTERVAL_MS = 2000
MACOS_CURSOR_REFRESH_MS = 50
DEFAULT_MAC_COMMAND_AS_CTRL = sys.platform == "darwin"
_MACOS_TRANSPARENT_CURSOR: Any = None
_MACOS_CURSOR_WARNING_LOGGED = False


def _set_native_macos_video_cursor(
    host_widget: Any, hidden: bool, currently_hidden: bool
) -> Optional[bool]:
    """Apply a transparent AppKit cursor only over the native video view.

    NSCursor.hide() is process-wide and macOS can reset its visible state while
    changing fullscreen UI.  A transparent NSCursor avoids that global hide
    count.  The native window hit-test also leaves the menu bar, Dock, title bar,
    and other windows free to display their normal cursors.
    """
    if sys.platform != "darwin":
        return None

    global _MACOS_TRANSPARENT_CURSOR, _MACOS_CURSOR_WARNING_LOGGED
    try:
        from ctypes import c_void_p

        import objc
        from AppKit import NSCursor, NSEvent, NSImage, NSPointInRect, NSWindow

        if not hidden:
            if currently_hidden:
                NSCursor.arrowCursor().set()
            return False

        ns_view = objc.objc_object(c_void_p=int(host_widget.winId()))
        ns_window = ns_view.window()
        if ns_window is None:
            if currently_hidden:
                NSCursor.arrowCursor().set()
            return False

        screen_point = NSEvent.mouseLocation()
        top_window_number = NSWindow.windowNumberAtPoint_belowWindowWithWindowNumber_(
            screen_point, 0
        )
        window_point = ns_window.convertPointFromScreen_(screen_point)
        view_point = ns_view.convertPoint_fromView_(window_point, None)
        over_video = int(top_window_number) == int(ns_window.windowNumber()) and bool(
            NSPointInRect(view_point, ns_view.visibleRect())
        )

        if not over_video:
            if currently_hidden:
                NSCursor.arrowCursor().set()
            return False

        if _MACOS_TRANSPARENT_CURSOR is None:
            transparent_image = NSImage.alloc().initWithSize_((16.0, 16.0))
            _MACOS_TRANSPARENT_CURSOR = NSCursor.alloc().initWithImage_hotSpot_(
                transparent_image, (0.0, 0.0)
            )
        # Reasserting the scoped cursor is intentional: native fullscreen
        # transitions can replace the current cursor without emitting a Qt
        # enter/move event when the physical pointer remains stationary.
        _MACOS_TRANSPARENT_CURSOR.set()
        return True
    except Exception as exc:  # pragma: no cover - macOS/PyObjC availability
        if not _MACOS_CURSOR_WARNING_LOGGED:
            logging.warning(f"Could not apply native macOS video cursor: {exc}")
            _MACOS_CURSOR_WARNING_LOGGED = True
        return None


def _forward_mouse_double_click(view, event: QMouseEvent) -> None:
    """Forward Qt's dedicated double-click event as the second button-down."""
    view._forward_mouse_press(event)
    event.accept()


def _forward_mouse_move(view, event: QMouseEvent, drag_distance: int) -> bool:
    """Emit movement unless it is sub-threshold jitter during a click.

    Qt's drag threshold is measured in viewport pixels. Keeping this decision
    before mapToScene is important for a scaled 4K stream, where one viewport
    pixel can become several absolute HID pixels.
    """
    if (
        view._mouse_press_pos is not None
        and event.buttons() != Qt.MouseButton.NoButton
        and not view._drag_started
    ):
        delta = event.pos() - view._mouse_press_pos
        if delta.manhattanLength() < drag_distance:
            return False
        view._drag_started = True

    scene_pos = view.mapToScene(event.pos())
    view.mouseMoved.emit(scene_pos.x(), scene_pos.y())
    return True


def _mouse_release_position(view, event: QMouseEvent):
    """Keep click jitter stationary, but preserve an intentional drag endpoint."""
    if view._mouse_press_pos is not None and not view._drag_started:
        return view._mouse_press_pos
    return event.pos()


# Subclass QGraphicsView so clicks inside the view can receive focus and
# emit signals that the main window can wire into its focus handlers.
class VideoGraphicsView(QGraphicsView):
    mousePressed = pyqtSignal(float, float, Qt.MouseButton, bool)
    mouseReleased = pyqtSignal(float, float, Qt.MouseButton, bool)
    mouseMoved = pyqtSignal(float, float)

    def __init__(self, scene=None, parent=None):
        super().__init__(scene, parent)
        # Set click focus policy to maintain focus on Tab
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # Remove this widget from the tab focus chain entirely
        self.setFocusProxy(None)
        # Enable mouse tracking
        self.setMouseTracking(True)
        self._mouse_press_pos = None
        self._drag_started = False
        self.main_window = None

        # Find and store reference to main window
        widget = self
        while widget and not isinstance(widget, KVMQtGui):
            widget = widget.parent()
        self.main_window = widget

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._forward_mouse_press(event)
        return super().mousePressEvent(event)

    def _forward_mouse_press(self, event: QMouseEvent) -> None:
        # Ensure the view receives focus when clicked so focus events fire
        self.setFocus()
        if self.main_window:
            self.main_window._pointer_over_video = True
            self.main_window._apply_mouse_cursor()
        self._mouse_press_pos = event.pos()
        self._drag_started = False
        # Convert to scene coordinates
        scene_pos = self.mapToScene(event.pos())
        self.mousePressed.emit(scene_pos.x(), scene_pos.y(), event.button(), True)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        # Qt sends the second press in a double-click as MouseButtonDblClick,
        # not MouseButtonPress. Forward it as a regular second button-down so
        # the remote receives press/release, press/release and recognises a
        # double click. The normal mouseReleaseEvent handles the second release.
        _forward_mouse_double_click(self, event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # A release after sub-threshold click jitter must use the original press
        # coordinate. This matters now that clicks carry their absolute position
        # in the same HID report; otherwise a tiny hand movement becomes a drag.
        release_pos = _mouse_release_position(self, event)
        scene_pos = self.mapToScene(release_pos)
        self.mouseReleased.emit(scene_pos.x(), scene_pos.y(), event.button(), False)
        if event.buttons() == Qt.MouseButton.NoButton:
            self._mouse_press_pos = None
            self._drag_started = False
        return super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        _forward_mouse_move(self, event, QApplication.startDragDistance())
        # logging.debug(f"View mouse move: {scene_pos.x():.1f}, {scene_pos.y():.1f}")
        result = super().mouseMoveEvent(event)
        if self.main_window and self.main_window.hide_mouse_var:
            self.main_window._pointer_over_video = True
            self.main_window._apply_mouse_cursor()
        return result

    def enterEvent(self, event: QEvent) -> None:
        if self.main_window:
            self.main_window._pointer_over_video = True
            self.main_window._apply_mouse_cursor()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if self.main_window and not self.main_window._quitting:
            self.main_window._pointer_over_video = False
            self.main_window._apply_mouse_cursor()
        super().leaveEvent(event)

    def focusInEvent(self, event: QFocusEvent) -> None:
        logging.info("Video view focused - keyboard capture enabled")
        if self.main_window and not self.main_window._quitting:
            self.main_window.keyboard_var = True
            self.main_window._apply_mouse_cursor()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        logging.info("Video view unfocused - keyboard capture disabled")
        if self.main_window:
            self.main_window.keyboard_var = False
            # QWidget emits focusOutEvent late while a closing NSWindow is being
            # hidden.  At that point native child views and the serial transport
            # may already be gone.  Never call them again during shutdown: an
            # exception escaping a PyQt virtual event handler makes Qt abort the
            # packaged process instead of exiting normally.
            if not self.main_window._quitting:
                try:
                    self.main_window._refresh_native_mouse_cursor(force_visible=True)
                    self.main_window._release_keyboard_capture()
                except Exception:
                    logging.exception("Error while releasing video focus")
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Let the main window handle the key event first
        if self.main_window:
            self.main_window.keyPressEvent(event)
        # Prevent Qt from using arrow keys for scrolling
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if self.main_window:
            self.main_window.keyReleaseEvent(event)
        event.accept()


class KVMQtGui(QMainWindow):
    """
    Main GUI class for the Serial KVM application (Qt version).

    A graphical user interface (GUI) for controlling software KVM (Keyboard, Video, Mouse) switches
    using CH9329 or CH9350L UART-to-USB-HID bridge chips.

    Provides a PyQt5-based interface for configuring and controlling serial, video, keyboard,
    and mouse devices. Handles device selection, status display, event processing, and persistent
    settings management for the SerialKVM tool.
    """

    CONFIG_FILE: str = ".kvm_settings.ini"

    baud_rates: list[int] = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
    serial_ports: list[str] = []
    video_devices: list = []

    keyboard_var: bool = False
    keyboard_last: str = ""
    video_var: int = -1
    mouse_var: bool = False

    serial_port_var: str = "Loading serial..."
    baud_rate_var: int = -1
    video_device_var: str = "Loading cameras..."
    keyboard_layout_var: str = "en_GB"
    resolution_var: str = ""  # "WIDTHxHEIGHT", empty means auto (from camera enumeration)
    # Protocol selection: "ch9329" (default) or "ch9350" with a working state
    # in {0, 2, 3, 4}. State 0 enters paired mode (descriptor handshake);
    # 2/3/4 are the dipswitch-fixed simple modes.
    protocol_var: str = "ch9329"
    ch9350_state_var: int = 2

    window_var: bool = False
    show_status_var: bool = False
    # Video scale: "fit" (fill view, preserve aspect) or a numeric string parsed as a
    # fixed pixel scale factor (e.g. "0.25", "0.5", "1", "2")
    scale_mode_var: str = "fit"
    status_var: str
    verbose_var: bool = False
    hide_mouse_var: bool = True
    mac_command_as_ctrl_var: bool = DEFAULT_MAC_COMMAND_AS_CTRL
    monitor_hdmi_audio_var: bool = False
    latency_diagnostics_var: bool = False

    _quitting: bool = False
    _pointer_over_video: bool = False
    _native_mouse_cursor_hidden: bool = False

    pos_x: int = 0
    pos_y: int = 0

    # IO
    serial_port: Serial | None = None
    keyboard_op: QtOp | None = None
    mouse_op: MouseOp | None = None
    # The DataCommManager owning the active comm + its lifecycle. Reset
    # alongside the serial port whenever __init_serial reopens the link.
    comm_manager: "DataCommManager | None" = None

    # Dimensions
    window_default_width: int = 1280
    window_default_height: int = 720
    window_min_width: int = 512
    window_min_height: int = 320
    status_bar_default_height: int = 24  # Typical status bar height in pixels

    # Video
    video_view: QGraphicsView
    video_scene: QGraphicsScene
    video_item: QGraphicsVideoItem
    qcamera: Optional[QCamera] = None  # Active QCamera instance (None until enumeration completes)
    native_capture: Optional[Any] = None  # macOS AVFoundation preview session

    # Status bar labels
    status_bar: QStatusBar
    status_serial_label: QLabel
    status_keyboard_label: QLabel
    status_mouse_label: QLabel
    status_video_label: QLabel

    # Utility dictionary for Mouse button handling
    BUTTON_MAP: dict = {
        Qt.MouseButton.MiddleButton: "MIDDLE",
        Qt.MouseButton.LeftButton: "LEFT",
        Qt.MouseButton.RightButton: "RIGHT",
    }

    def __init__(self) -> None:
        """
        Initialise the KVMQtGui application window, UI elements, variables, menus, and event bindings.
        """
        super().__init__()

        # Initialise state variables
        self.baud_rate_var = self.baud_rates[3]  # Default to 9600
        self._pointer_over_video = False
        self._native_mouse_cursor_hidden = False
        self._reset_latency_diagnostics()

        # Perform initialisation
        self.__init_window()
        self.__init_menu()
        self.__init_status_bar()
        self.__init_video()
        self.__init_timers()

    def __init_window(self):
        # Window characteristics
        self.setWindowTitle("Serial KVM")
        self.setMinimumSize(
            self.window_min_width, self.window_min_height + self.status_bar_default_height
        )
        self.resize(
            self.window_default_width, self.window_default_height + self.status_bar_default_height
        )

        # Set up main layout
        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # Create central widget to hold layout
        self.central_widget = QWidget()
        self.central_widget.setLayout(self.main_layout)
        self.setCentralWidget(self.central_widget)

        # Make sure the window can receive key events
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def __init_menu(self):

        # Menu Bar
        menubar = self.menuBar()
        # self.setMenuBar(menubar)

        if menubar is None:
            raise TypeError("menubar must be QMenu, not None")

        # addMenu returning None is extremely unlikely in normal desktop apps.
        # The static type stubs for PyQt sometimes mark returns Optional, so
        # type-checkers warn even though runtime None is unlikely.
        # So, while we do check for menubar being None, we can just cast the menus.

        # File Menu
        file_menu = menubar.addMenu("File")
        file_menu = cast(QMenu, file_menu)  # addMenu type annotation is Optional
        save_action = QAction("Save Configuration", self)
        save_action.triggered.connect(self._save_settings)
        file_menu.addAction(save_action)

        # About
        about_action = QAction("About Serial KVM", self)
        about_action.triggered.connect(self._show_about)
        file_menu.addAction(about_action)

        # Quit
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._on_quit)
        file_menu.addAction(quit_action)

        # Edit Menu
        edit_menu = menubar.addMenu("Edit")
        edit_menu = cast(QMenu, edit_menu)
        self.paste_action = QAction("Paste", self)
        self.paste_action.triggered.connect(self._on_paste)
        edit_menu.addAction(self.paste_action)

        # Screenshot
        screenshot_action = QAction("Take Screenshot", self)
        screenshot_action.triggered.connect(self._take_screenshot)
        edit_menu.addAction(screenshot_action)

        # Add CTRL+ALT+DEL action
        ctrl_alt_del_action = QAction("Send CTRL+ALT+DEL", self)
        ctrl_alt_del_action.triggered.connect(self._send_ctrl_alt_del)
        edit_menu.addAction(ctrl_alt_del_action)

        # Options Menu
        options_menu = menubar.addMenu("Options")
        options_menu = cast(QMenu, options_menu)  # hush PyLance

        # Serial Port, Baud, Video, Resolution, Keyboard Layout, Protocol submenus
        self.serial_port_menu = options_menu.addMenu("Serial Port")
        self.baud_rate_menu = options_menu.addMenu("Baud Rate")
        self.video_device_menu = options_menu.addMenu("Video Device")
        self.resolution_menu = options_menu.addMenu("Resolution")
        self.keyboard_layout_menu = options_menu.addMenu("Keyboard Layout")
        self.protocol_menu = options_menu.addMenu("Protocol")
        self.audio_menu = options_menu.addMenu("Audio")

        self.hdmi_audio_action = QAction("Monitor HDMI Audio", self)
        self.hdmi_audio_action.setCheckable(True)
        self.hdmi_audio_action.setChecked(self.monitor_hdmi_audio_var)
        self.hdmi_audio_action.setEnabled(sys.platform == "darwin")
        self.hdmi_audio_action.setStatusTip(
            "Play audio from the selected HDMI capture device on the default macOS output"
        )
        self.hdmi_audio_action.triggered.connect(self._toggle_hdmi_audio)
        self.audio_menu.addAction(self.hdmi_audio_action)

        options_menu.addSeparator()

        self.mac_command_as_ctrl_action = QAction("Mac Command as Windows Ctrl", self)
        self.mac_command_as_ctrl_action.setCheckable(True)
        self.mac_command_as_ctrl_action.setChecked(self.mac_command_as_ctrl_var)
        self.mac_command_as_ctrl_action.triggered.connect(self._toggle_mac_command_as_ctrl)
        options_menu.addAction(self.mac_command_as_ctrl_action)

        # Verbose Logging option
        self.verbose_action = QAction("Verbose Logging", self)
        self.verbose_action.setCheckable(True)
        self.verbose_action.setChecked(self.verbose_var)
        self.verbose_action.triggered.connect(self._toggle_verbose)
        options_menu.addAction(self.verbose_action)

        self.latency_diagnostics_action = QAction("Latency Diagnostics", self)
        self.latency_diagnostics_action.setCheckable(True)
        self.latency_diagnostics_action.setChecked(self.latency_diagnostics_var)
        self.latency_diagnostics_action.setStatusTip(
            "Log input queue and serial dispatch latency without inspecting video frames"
        )
        self.latency_diagnostics_action.triggered.connect(self._toggle_latency_diagnostics)
        options_menu.addAction(self.latency_diagnostics_action)

        # View menu
        view_menu = menubar.addMenu("View")
        view_menu = cast(QMenu, view_menu)  # hush PyLance
        self.status_action = QAction("Show Status Bar", self)
        self.status_action.setCheckable(True)
        self.status_action.setChecked(self.show_status_var)

        def _toggle_status():
            logging.info("Toggling status bar visibility")
            self.show_status_var = not self.show_status_var
            self.status_bar.setVisible(self.show_status_var)

        self.status_action.triggered.connect(_toggle_status)
        view_menu.addAction(self.status_action)

        # Hide Mouse Pointer option
        self.mouse_action = QAction("Hide Mouse Pointer", self)
        self.mouse_action.setCheckable(True)
        self.mouse_action.setChecked(self.hide_mouse_var)
        self.mouse_action.triggered.connect(self._toggle_mouse)
        view_menu.addAction(self.mouse_action)

        # Scale Video submenu
        self.scale_menu = cast(QMenu, view_menu.addMenu("Scale Video"))
        self._scale_actions: dict[str, QAction] = {}
        for label, mode in [
            ("Dynamic (Fit to Window)", "fit"),
            ("1:4 ratio", "0.25"),
            ("1:2 ratio", "0.5"),
            ("1:1 ratio", "1"),
            ("2:1 ratio", "2"),
        ]:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(mode == self.scale_mode_var)
            action.triggered.connect(lambda _checked, m=mode: self._on_scale_mode_selected(m))
            self.scale_menu.addAction(action)
            self._scale_actions[mode] = action

        resize_action = QAction("Resize Window to Video", self)
        resize_action.triggered.connect(self._on_resize_window_to_resolution)
        view_menu.addAction(resize_action)
        view_menu.addSeparator()

        # Fullscreen toggle (macOS provides its own native fullscreen via the green
        # traffic light button and "Enter Full Screen" menu item automatically)
        if sys.platform != "darwin":
            fullscreen_action = QAction("Fullscreen", self)
            fullscreen_action.setCheckable(True)
            fullscreen_action.setShortcut("F11")

            def _toggle_fullscreen():
                if self.isFullScreen():
                    self.showNormal()
                    fullscreen_action.setChecked(False)
                else:
                    self.showFullScreen()
                    fullscreen_action.setChecked(True)

            fullscreen_action.triggered.connect(_toggle_fullscreen)
            view_menu.addAction(fullscreen_action)

            passthrough_action = QAction("Pass Through F11", self)
            passthrough_action.setCheckable(True)

            def _toggle_passthrough(checked):
                fullscreen_action.setShortcut("" if checked else "F11")

            passthrough_action.triggered.connect(_toggle_passthrough)
            view_menu.addAction(passthrough_action)

        logging.debug(f"Menus created")

    def __init_status_bar(self):
        # Status Bar
        self.status_bar = QStatusBar()

        # Create 4 labels for the sections
        self.status_serial_label = QLabel(self.serial_port_var)
        self.status_keyboard_label = QLabel("Keyboard: Idle")
        self.status_mouse_label = QLabel("Mouse: Idle")
        self.status_video_label = QLabel(self.video_device_var)

        # Add labels to status bar with equal stretch
        self.status_bar.addWidget(self.status_serial_label, 1)
        self.status_bar.addWidget(self.status_keyboard_label, 1)
        self.status_bar.addWidget(self.status_mouse_label, 1)
        self.status_bar.addWidget(self.status_video_label, 1)

        # Set as window's status bar
        self.setStatusBar(self.status_bar)
        self.status_bar.setVisible(self.show_status_var)

        # Style the labels for better visibility
        for label in [
            self.status_serial_label,
            self.status_keyboard_label,
            self.status_mouse_label,
            self.status_video_label,
        ]:
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            label.setStyleSheet("QLabel { border: 1px solid gray; }")

    def __init_video(self):
        # Video Display Area (QGraphicsView)
        self.video_scene = QGraphicsScene(self)
        # Use subclassed view so clicks/focus inside the view can be handled explicitly
        self.video_view = VideoGraphicsView(self.video_scene, self)
        self.video_view.setStyleSheet("background-color: black;")
        self.video_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.video_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Make view resize its scene automatically
        self.video_view.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.video_view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.video_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # QGraphicsVideoItem is QtMultimedia's video sink that renders directly into
        # a QGraphicsScene. The QCamera (created later, once enumeration completes)
        # streams frames into it via setViewfinder(self.video_item).
        self.video_item = QGraphicsVideoItem()
        self.video_item.setSize(QSizeF(self.window_default_width, self.window_default_height))
        self.video_scene.addItem(self.video_item)
        self.video_scene.setSceneRect(self.video_item.boundingRect())
        # Native size is reported asynchronously after the camera starts streaming.
        self.video_item.nativeSizeChanged.connect(self._on_video_native_size_changed)
        self.native_capture = None
        self._pending_mouse_move: Optional[tuple[int, int, int, int]] = None
        self._pending_mouse_event_at: Optional[float] = None
        self._last_mouse_report_at = 0.0

        # Add video view to main layout
        self.main_layout.addWidget(self.video_view, 1)  # 1 = stretch factor

        # Set mouse tracking on video view
        self.video_view.setMouseTracking(True)

        # Give the window a chance to show and lay out its widgets
        QApplication.processEvents()

        # Wire focus signals from the view back to the main window handlers.
        # Connect view-local focus signals to dedicated handlers so the
        # keyboard capture state is only affected by focusing inside the view.
        try:
            # Connect mouse signals from view to handlers
            self.video_view.mousePressed.connect(self._on_mouse_click)
            self.video_view.mouseReleased.connect(self._on_mouse_click)
            self.video_view.mouseMoved.connect(self._on_mouse_move)
        except (AttributeError, TypeError) as e:
            logging.warning(f"Could not connect video view mouse signals: {e}")

    def __init_timers(self):
        # QtMultimedia owns the frame pipeline; no per-frame Python timer is needed.
        # Defer device enumeration and settings load past the event-loop start.
        # Both run in the same deferred callback so _load_settings always sees a
        # fully-populated video_devices list — a fixed 10ms timer would race with
        # _wait_for_loaded()'s inner QEventLoop during camera probing.
        QTimer.singleShot(0, self.__init_devices)

        # Status bar timer
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_status_bar)
        self.status_timer.start(500)  # Update every half second

        # One-shot, event-driven mouse reporting. The timer is armed only when
        # UART pacing requires it; after idle, movement is sent immediately.
        self.mouse_report_timer = QTimer()
        self.mouse_report_timer.setSingleShot(True)
        self.mouse_report_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.mouse_report_timer.timeout.connect(self._flush_pending_mouse_move)

        # This timer is stopped unless diagnostics are explicitly enabled. The
        # measurements stay out of the AVFoundation frame path, so profiling
        # input cannot introduce a video-frame callback or a 4K pixel copy.
        self.latency_diagnostics_timer = QTimer()
        self.latency_diagnostics_timer.timeout.connect(self._log_latency_diagnostics)

        # AppKit/Qt can replace the cursor during a native fullscreen transition
        # without sending another mouse event.  Reassert a transparent cursor at
        # low cost, after verifying that this window's video NSView is actually
        # the topmost native content below the physical pointer.
        if sys.platform == "darwin":
            self.cursor_refresh_timer = QTimer()
            self.cursor_refresh_timer.setTimerType(Qt.TimerType.PreciseTimer)
            self.cursor_refresh_timer.timeout.connect(self._refresh_native_mouse_cursor)
            self.cursor_refresh_timer.start(MACOS_CURSOR_REFRESH_MS)

    def __init_devices(self):
        """
        Initialise and populate device lists (serial ports, video devices, keyboard layouts),
        then load saved settings. Settings load is intentionally last so it always sees
        complete device lists; calling it from a separate timer would race with the inner
        QEventLoop spun by _wait_for_loaded() during camera probing.
        """
        self._populate_serial_ports()
        self._populate_baud_rates()
        self._populate_video_devices()
        self._populate_keyboard_layouts()
        self._populate_protocol_menu()
        QTimer.singleShot(10, lambda: self._load_settings(self.CONFIG_FILE))

    def _update_status_bar(self):
        """
        Update the status bar with current serial, keyboard, mouse, and video device information.
        """
        if not self.show_status_var:
            return

        # Update each status bar part
        self.status_serial_label.setText(
            f"Serial: {self.serial_port_var} @{self.baud_rate_var} baud"
        )

        captured = "Captured" if self.keyboard_var else "Idle"
        self.status_keyboard_label.setText(f"Keyboard: {captured} {self.keyboard_last}")

        camera_width, camera_height = self._camera_resolution()
        report = f"Mouse: [x:{self.pos_x} y:{self.pos_y}] in [{camera_width}x{camera_height}]"
        self.status_mouse_label.setText(report)
        idx = self.video_var

        if idx >= 0 and idx < len(self.video_devices):
            native_rate = ""
            if self.native_capture is not None and self.native_capture.fps > 0:
                native_rate = f" @ {self.native_capture.fps:.2f} fps"
            self.status_video_label.setText(f"Video: {str(self.video_devices[idx])}{native_rate}")
        else:
            # Show video_device_var status (e.g., "Initialising...", "None found", "Error")
            # instead of hardcoded "Idle" when no camera is selected
            self.status_video_label.setText(f"Video: {self.video_device_var}")

    def _toggle_verbose(self):
        """Toggle verbose logging and update log level."""
        self.verbose_var = not self.verbose_var
        self.verbose_action.setChecked(self.verbose_var)
        self._apply_log_level()

    def _apply_log_level(self):
        if self.verbose_var:
            logging.getLogger().setLevel(logging.DEBUG)
            logging.debug("Verbose logging enabled.")
        else:
            logging.getLogger().setLevel(logging.INFO)
            logging.info("Verbose logging disabled.")

    @staticmethod
    def _latency_percentiles(samples: list[float]) -> str:
        """Format millisecond p50/p95/max values for a short diagnostics line."""
        if not samples:
            return "n/a"
        ordered = sorted(samples)

        def percentile(fraction: float) -> float:
            index = max(0, math.ceil(len(ordered) * fraction) - 1)
            return ordered[index]

        return (
            f"p50={percentile(0.50):.2f}ms " f"p95={percentile(0.95):.2f}ms max={ordered[-1]:.2f}ms"
        )

    def _reset_latency_diagnostics(self) -> None:
        """Reset the current time window used by the opt-in input profiler."""
        self._latency_started_at = time.monotonic()
        self._latency_mouse_events = 0
        self._latency_mouse_sent = 0
        self._latency_mouse_coalesced = 0
        self._latency_mouse_queue_ms: list[float] = []
        self._latency_mouse_dispatch_ms: list[float] = []
        self._latency_click_dispatch_ms: list[float] = []
        self._latency_wheel_dispatch_ms: list[float] = []
        self._latency_keyboard_dispatch_ms: list[float] = []

    def _toggle_latency_diagnostics(self, checked: bool) -> None:
        """Start or stop low-overhead input latency aggregation."""
        requested = bool(checked)
        self.latency_diagnostics_action.setChecked(requested)
        if requested:
            self.latency_diagnostics_var = True
            self._reset_latency_diagnostics()
            self.latency_diagnostics_timer.start(LATENCY_DIAGNOSTICS_INTERVAL_MS)
            logging.info(
                "Latency diagnostics enabled; input metrics will be logged every %.1f seconds",
                LATENCY_DIAGNOSTICS_INTERVAL_MS / 1000,
            )
        else:
            self._log_latency_diagnostics()
            self.latency_diagnostics_var = False
            self.latency_diagnostics_timer.stop()
            logging.info("Latency diagnostics disabled")

    def _log_latency_diagnostics(self) -> None:
        """Log one input-latency window and begin a fresh aggregation window."""
        if not self.latency_diagnostics_var:
            return

        elapsed = max(0.001, time.monotonic() - self._latency_started_at)
        coalesced_percent = (
            100.0 * self._latency_mouse_coalesced / self._latency_mouse_events
            if self._latency_mouse_events
            else 0.0
        )
        baud_rate = max(1, int(self.baud_rate_var))
        uart_mouse_ms = 1000.0 * MOUSE_ABSOLUTE_REPORT_BITS / baud_rate
        uart_keyboard_ms = 1000.0 * KEYBOARD_REPORT_BITS / baud_rate
        logging.info(
            "LATENCY %.1fs | mouse events=%d sent=%d coalesced=%d (%.1f%%) "
            "queue[%s] dispatch[%s] | click[%s] wheel[%s] keyboard[%s] | "
            "UART packets mouse=%.2fms keyboard=%.2fms @%d baud",
            elapsed,
            self._latency_mouse_events,
            self._latency_mouse_sent,
            self._latency_mouse_coalesced,
            coalesced_percent,
            self._latency_percentiles(self._latency_mouse_queue_ms),
            self._latency_percentiles(self._latency_mouse_dispatch_ms),
            self._latency_percentiles(self._latency_click_dispatch_ms),
            self._latency_percentiles(self._latency_wheel_dispatch_ms),
            self._latency_percentiles(self._latency_keyboard_dispatch_ms),
            uart_mouse_ms,
            uart_keyboard_ms,
            baud_rate,
        )
        self._reset_latency_diagnostics()

    def _load_settings(self, config_file: str):
        """
        Load settings and set variables (deferred).
        """
        kvm = settings_util.load_settings(config_file, "KVM")

        if (
            self.video_device_menu is None
            or self.baud_rate_menu is None
            or self.serial_port_menu is None
            or self.resolution_menu is None
        ):
            raise TypeError("Initialise all menus before calling _load_settings")

        # Load serial port setting (only if present in current options)
        if kvm.get("serial_port") in self.serial_ports:
            self.serial_port_var = kvm.get("serial_port", self.serial_ports[-1])
            # Update menu selection
            for action in self.serial_port_menu.actions():
                action.setChecked(action.text() == self.serial_port_var)

        # Load baud rate setting (only if valid)
        if kvm.get("baud_rate") and int(kvm.get("baud_rate", "")) in self.baud_rates:
            self.baud_rate_var = int(kvm.get("baud_rate", ""))
            # Update menu selection
            for action in self.baud_rate_menu.actions():
                action.setChecked(action.text() == str(self.baud_rate_var))

        # Load video device setting: update video_var and menu checkmark.
        # Camera opening is deferred until after resolution_var is known below
        # so both can be applied in a single _set_camera call.
        if kvm.get("video_device") is not None:
            try:
                idx = int(kvm.get("video_device", 0))
                if 0 <= idx < len(self.video_devices):
                    self.video_var = idx
                    self.video_device_var = str(self.video_devices[idx])
                    for action in self.video_device_menu.actions():
                        action.setChecked(action.text() == self.video_device_var)
            except (ValueError, TypeError, IndexError):
                logging.warning(
                    f"Invalid video device index in settings: {kvm.get('video_device')}"
                )
        elif self.video_devices:
            self.video_var = 0

        # Load resolution_var before opening the camera so _populate_resolution_menu
        # (below) can apply it in a single _set_camera call rather than opening the
        # camera twice — once without resolution and once with.
        saved_res = kvm.get("resolution", "")
        if saved_res:
            parts = saved_res.split("x")
            try:
                w, h = int(parts[0]), int(parts[1])
                if w > 0 and h > 0:
                    self.resolution_var = f"{w}x{h}"
                else:
                    logging.warning(f"Invalid resolution in settings (zero/negative): {saved_res}")
            except (ValueError, IndexError):
                logging.warning(f"Invalid resolution in settings: {saved_res}")

        # Load this before opening the camera. The video session starts first;
        # audio monitoring is then opened in its own independent session.
        self.monitor_hdmi_audio_var = kvm.get("monitor_hdmi_audio", "False") == "True"

        # Open the camera. _populate_resolution_menu rebuilds the menu for the active
        # device and applies resolution_var in a single _set_camera call when the
        # resolution is supported. If resolution_var is empty or unsupported it clears
        # it without opening the camera, so we fall back to the device default.
        if self.video_devices:
            self._populate_resolution_menu(self.video_var)
            if not self.resolution_var:
                self._set_camera(self.video_devices[self.video_var])

        # Load other boolean settings
        self.window_var = kvm.get("windowed", "False") == "True"
        self.verbose_var = kvm.get("verbose", "False") == "True"
        self.show_status_var = kvm.get("statusbar", "False") == "True"
        self.hide_mouse_var = kvm.get("hide_mouse", "True") == "True"
        shortcut_default = "True" if DEFAULT_MAC_COMMAND_AS_CTRL else "False"
        self.mac_command_as_ctrl_var = kvm.get("mac_command_as_ctrl", shortcut_default) == "True"

        # Load keyboard layout, auto-detect if not previously configured
        if "keyboard_layout" in kvm:
            self.keyboard_layout_var = kvm.get("keyboard_layout")
        else:
            # Auto-detect from system locale on first run
            self.keyboard_layout_var = self._detect_system_keyboard_layout()
            logging.info(f"Auto-detected keyboard layout: {self.keyboard_layout_var}")

        # Load protocol selection (default CH9329 if missing or invalid)
        saved_protocol = kvm.get("protocol", "ch9329")
        if saved_protocol == "ch9350":
            try:
                saved_state = int(kvm.get("ch9350_state", "2"))
            except ValueError:
                saved_state = 2
            if saved_state in (0, 2, 3, 4):
                self.protocol_var = "ch9350"
                self.ch9350_state_var = saved_state
            else:
                logging.warning(
                    f"Invalid ch9350_state in settings: {kvm.get('ch9350_state')}; "
                    "falling back to CH9329"
                )
                self.protocol_var = "ch9329"
        else:
            self.protocol_var = "ch9329"
        if hasattr(self, "protocol_menu") and self.protocol_menu is not None:
            target = self._protocol_label(self.protocol_var, self.ch9350_state_var)
            for action in self.protocol_menu.actions():
                action.setChecked(action.text() == target)

        # Apply mouse cursor state if needed
        if hasattr(self, "video_view"):
            self._apply_mouse_cursor()
        # Set the checked state of the menu item if it exists
        if hasattr(self, "mouse_action"):
            self.mouse_action.setChecked(self.hide_mouse_var)
        if hasattr(self, "status_action"):
            self.status_action.setChecked(self.show_status_var)
        if hasattr(self, "status_bar"):
            self.status_bar.setVisible(self.show_status_var)
        if hasattr(self, "mac_command_as_ctrl_action"):
            self.mac_command_as_ctrl_action.setChecked(self.mac_command_as_ctrl_var)
        if hasattr(self, "hdmi_audio_action"):
            self.hdmi_audio_action.setChecked(self.monitor_hdmi_audio_var)
        # And for verbose logging
        if hasattr(self, "verbose_action"):
            self.verbose_action.setChecked(self.verbose_var)
            self._apply_log_level()
        # And for keyboard layout
        if hasattr(self, "keyboard_layout_menu"):
            for action in self.keyboard_layout_menu.actions():
                action.setChecked(action.text() == self.keyboard_layout_var)

        # Initialise serial operations with loaded settings
        self.__init_serial()

        logging.info("Settings loaded from configuration file.")

    def _take_screenshot(self):
        """
        Capture the current video frame and save to clipboard/file.

        Copies the frame to the clipboard, then opens a file dialog for
        saving to disk. If the user cancels the dialog, the frame is
        still available on the clipboard.
        """
        # Grab whatever's currently rendered in the video view (post-scaling).
        # Renders at the camera's native resolution when available, fitting the
        # scene rect set by _on_video_native_size_changed.
        pixmap = self._grab_video_frame()
        if pixmap is None or pixmap.isNull():
            QMessageBox.warning(self, "Screenshot", "No video frame available to capture.")
            return

        # Copy to clipboard
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setPixmap(pixmap)

        # Offer file save dialog
        default_name = time.strftime("kvm_screenshot_%Y%m%d_%H%M%S.png")
        default_dir = os.path.expanduser("~")
        default_path = os.path.join(default_dir, default_name)

        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "Save Screenshot",
            default_path,
            "PNG Image (*.png)",
        )

        if filepath:
            if pixmap.save(filepath, "PNG"):
                QMessageBox.information(
                    self,
                    "Screenshot",
                    f"Screenshot saved to:\n{filepath}\n\n"
                    "The image has also been copied to the clipboard.",
                )
            else:
                logging.error(f"Failed to save screenshot to {filepath}")
                QMessageBox.warning(
                    self,
                    "Screenshot",
                    f"Failed to save screenshot to:\n{filepath}\n\n"
                    "The image has been copied to the clipboard.",
                )
        else:
            QMessageBox.information(
                self,
                "Screenshot",
                "Screenshot copied to clipboard.",
            )

    def _save_settings(self):
        """
        Save current application settings to the configuration file.
        """
        settings_dict = {
            "serial_port": self.serial_port_var,
            "video_device": str(self.video_var),
            "baud_rate": str(self.baud_rate_var),
            "resolution": self.resolution_var,
            "windowed": str(self.window_var),
            "statusbar": str(self.show_status_var),
            "verbose": str(self.verbose_var),
            "hide_mouse": str(self.hide_mouse_var),
            "mac_command_as_ctrl": str(self.mac_command_as_ctrl_var),
            "monitor_hdmi_audio": str(self.monitor_hdmi_audio_var),
            "keyboard_layout": str(self.keyboard_layout_var),
            "protocol": self.protocol_var,
            "ch9350_state": str(self.ch9350_state_var),
        }
        settings_util.save_settings(self.CONFIG_FILE, "KVM", settings_dict)
        logging.info("Settings saved to INI file.")
        QMessageBox.information(self, "Save", "Configuration saved.")

    def _populate_serial_ports(self):
        """
        Populate the list of available serial ports and update the menu.
        """
        try:
            self.serial_ports = list_serial_ports()
            logging.info(f"Found serial ports: {self.serial_ports}")

            if len(self.serial_ports) == 0:
                self.serial_port_var = "None found"
                self._populate_serial_port_menu()
                QMessageBox.warning(
                    self,
                    "Start-up Warning",
                    "No serial ports found.\n\n"
                    "Please ensure your USB serial device is connected and drivers are installed.\n"
                    "See documentation for driver installation and troubleshooting instructions.",
                )
            else:
                # Default to the last port found. Set BEFORE menu build so the
                # checkmark loop in _populate_serial_port_menu sees it.
                self.serial_port_var = self.serial_ports[-1]
                self._populate_serial_port_menu()

        except Exception as e:
            logging.error(f"Error discovering serial ports: {e}")
            QMessageBox.critical(self, "Error", f"Failed to discover serial ports: {e}")
            self.serial_ports = []
            self.serial_port_var = "Error"

    def _populate_serial_port_menu(self):
        """
        Populate the serial port dropdown menu with available serial ports.
        """
        if self.serial_port_menu is None:
            raise TypeError(
                "Initialise serial_port_menu before calling _populate_serial_port_menu()"
            )

        self.serial_port_menu.clear()
        for port in self.serial_ports:
            action = QAction(port, self)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, p=port: self._on_serial_port_selected(p))
            self.serial_port_menu.addAction(action)

            # Check the current selection
            if port == self.serial_port_var:
                action.setChecked(True)

    def _populate_baud_rates(self):
        """
        Populate the baud rate menu with available baud rates.
        """
        if self.baud_rate_menu is None:
            raise TypeError("Initialise baud_rate_menu before calling _populate_baud_rates()")

        self.baud_rate_menu.clear()
        for rate in self.baud_rates:
            action = QAction(str(rate), self)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, r=rate: self._on_baud_rate_selected(r))
            self.baud_rate_menu.addAction(action)

            # Check the current selection
            if rate == self.baud_rate_var:
                action.setChecked(True)

    def _on_serial_port_selected(self, port):
        """
        Handle selection of a serial port.
        """
        if self.serial_port_menu is None:
            raise TypeError("Initialise serial_port_menu before calling _on_serial_port_selected()")

        # Uncheck all other serial port actions
        for action in self.serial_port_menu.actions():
            action.setChecked(action.text() == port)

        self.serial_port_var = port
        logging.info(f"Selected serial port: {port}")
        self.__init_serial()

    def _on_baud_rate_selected(self, baud_rate):
        """
        Handle selection of a baud rate.
        """
        if self.baud_rate_menu is None:
            raise TypeError("Initialise baud_rate_menu before calling _on_baud_rate_selected()")

        # Uncheck all other baud rate actions
        for action in self.baud_rate_menu.actions():
            action.setChecked(action.text() == str(baud_rate))

        self.baud_rate_var = baud_rate
        logging.info(f"Selected baud rate: {baud_rate}")
        self.__init_serial()

    def _populate_keyboard_layouts(self):
        """
        Populate the keyboard layout menu with available layouts.
        """
        if self.keyboard_layout_menu is None:
            raise TypeError(
                "Initialise keyboard_layout_menu before calling _populate_keyboard_layouts()"
            )

        from kvm_serial.utils import get_available_layouts

        self.keyboard_layout_menu.clear()
        for layout in get_available_layouts():
            action = QAction(layout, self)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, l=layout: self._on_keyboard_layout_selected(l))
            self.keyboard_layout_menu.addAction(action)

            # Check the current selection
            if layout == self.keyboard_layout_var:
                action.setChecked(True)

    # The full set of options the Protocol submenu offers. Each entry is
    # (label, protocol, ch9350_state). State is unused for CH9329 but kept
    # here for menu uniformity.
    _PROTOCOL_OPTIONS = [
        ("CH9329", "ch9329", -1),
        ("CH9350L (state 0/1, paired)", "ch9350", 0),
        ("CH9350L (state 2, BIOS)", "ch9350", 2),
        ("CH9350L (state 3, abs mouse)", "ch9350", 3),
        ("CH9350L (state 4, Digitizers)", "ch9350", 4),
    ]

    def _protocol_label(self, protocol: str, state: int) -> str:
        """Return the menu label corresponding to (protocol, state)."""
        for label, p, s in self._PROTOCOL_OPTIONS:
            if p == protocol and (p == "ch9329" or s == state):
                return label
        return self._PROTOCOL_OPTIONS[0][0]  # fall back to CH9329

    def _populate_protocol_menu(self):
        """
        Populate the Protocol submenu with checkable items for CH9329 and
        the four CH9350L working modes.
        """
        if self.protocol_menu is None:
            raise TypeError("Initialise protocol_menu before calling _populate_protocol_menu()")

        self.protocol_menu.clear()
        current = self._protocol_label(self.protocol_var, self.ch9350_state_var)
        for label, protocol, state in self._PROTOCOL_OPTIONS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked, p=protocol, s=state: self._on_protocol_selected(p, s)
            )
            self.protocol_menu.addAction(action)
            if label == current:
                action.setChecked(True)

    def _on_protocol_selected(self, protocol: str, state: int):
        """Handle selection of a protocol/state from the Protocol submenu."""
        if self.protocol_menu is None:
            raise TypeError("Initialise protocol_menu before calling _on_protocol_selected()")

        target_label = self._protocol_label(protocol, state)
        for action in self.protocol_menu.actions():
            action.setChecked(action.text() == target_label)

        self.protocol_var = protocol
        if protocol == "ch9350":
            self.ch9350_state_var = state
        logging.info(f"Selected protocol: {target_label}")
        self.__init_serial()

    def _on_keyboard_layout_selected(self, layout):
        """
        Handle selection of a keyboard layout.
        """
        if self.keyboard_layout_menu is None:
            raise TypeError(
                "Initialise keyboard_layout_menu before calling _on_keyboard_layout_selected()"
            )

        # Uncheck all other layout actions
        for action in self.keyboard_layout_menu.actions():
            action.setChecked(action.text() == layout)

        self.keyboard_layout_var = layout
        logging.info(f"Selected keyboard layout: {layout}")
        self.__init_serial()

    def _build_comm_cls(self):
        """
        Resolve the active protocol/state into a DataComm-producing callable
        for DataCommManager to instantiate. CH9329Comm is callable directly;
        CH9350Comm needs the chosen state bound via a lambda.
        """
        if self.protocol_var == "ch9350":
            from kvm_serial.utils.ch9350 import CH9350Comm

            state = self.ch9350_state_var
            return lambda port: CH9350Comm(port, state=state)

        from kvm_serial.utils.ch9329 import CH9329Comm

        return CH9329Comm

    def _stop_comm_manager(self):
        """Stop and discard the active DataCommManager, if any."""
        from kvm_serial.backend.manager import DataCommManager

        if self.comm_manager is not None:
            try:
                self.comm_manager.stop()
            except Exception as e:
                logging.error(f"Error stopping DataCommManager: {e}")
            self.comm_manager = None
        DataCommManager.reset()

    def _release_keyboard_capture(self) -> None:
        """Release held remote keys without letting shutdown/focus errors escape Qt."""
        if self.keyboard_op is None:
            return
        try:
            self.keyboard_op.release_all()
        except Exception as exc:
            logging.warning(f"Could not release held keys: {exc}")

    def __init_serial(self):
        """
        Initialise or reinitialise serial port, DataCommManager, and the
        keyboard/mouse operations bound to the shared comm.
        """
        # Stop the active manager (if any) before closing the underlying port.
        self._stop_comm_manager()
        self._close_serial_port()

        # Clear existing operations
        self.keyboard_op = None
        self.mouse_op = None

        # Only initialise if we have both port and valid baud rate
        if (
            self.serial_port_var
            and self.serial_port_var not in ["Loading serial...", "None found", "Error"]
            and self.baud_rate_var in self.baud_rates
        ):

            try:
                # Initialise serial port
                self.serial_port = Serial(self.serial_port_var, self.baud_rate_var)
                logging.info(
                    f"Opened serial port {self.serial_port_var} at {self.baud_rate_var} baud"
                )

                # Construct the DataCommManager for this port. Ops fetch the
                # shared comm via DataCommManager.get(); the manager owns
                # comm lifecycle (start/stop) so individual ops don't.
                from kvm_serial.backend.manager import DataCommManager

                self.comm_manager = DataCommManager(
                    self.serial_port, comm_cls=self._build_comm_cls()
                )
                self.comm_manager.start()
                logging.info(
                    f"DataCommManager started: protocol={self.protocol_var}"
                    + (f" (state {self.ch9350_state_var})" if self.protocol_var == "ch9350" else "")
                )

                # Initialise keyboard and mouse operations
                self.keyboard_op = QtOp(
                    self.serial_port,
                    layout=self.keyboard_layout_var,
                    macos_command_as_ctrl=self.mac_command_as_ctrl_var,
                )
                self.mouse_op = MouseOp(self.serial_port)
                logging.info("Initialised keyboard and mouse operations")

            except Exception as e:
                logging.error(f"Failed to initialise serial operations: {e}")
                QMessageBox.critical(
                    self, "Serial Error", f"Failed to open serial port {self.serial_port_var}:\n{e}"
                )
                # Reset to None if initialisation failed
                self._stop_comm_manager()
                self.serial_port = None
                self.keyboard_op = None
                self.mouse_op = None

    def _detect_system_keyboard_layout(self) -> str:
        """
        Auto-detect keyboard layout based on system locale using QLocale.
        Recognizes en_US and en_GB variants. Extend in future to detect more keyboards.

        Returns:
            str: Detected keyboard layout ('en_US' or 'en_GB'), defaults to 'en_GB'
        """

        default_layout = "en_GB"
        try:
            # Use QLocale for detection
            system_locale = QLocale.system()
            language = system_locale.language()  # 31 - English
            country = system_locale.country()  # 225- US; 224- GB

            # Map Qt locale to keyboard layout
            if language == QLocale.English:
                # US English -> en_US layout
                if country == QLocale.UnitedStates:
                    return "en_US"
                # All other English variants default to en_GB
                return default_layout
            else:
                # Non-English locales default to en_GB
                logging.debug(
                    f"System locale {system_locale.name()} is not English, defaulting to {default_layout}"
                )
                return default_layout
        except (AttributeError, ValueError) as e:
            logging.warning(
                f"Failed to auto-detect keyboard layout: {e}, defaulting to {default_layout}"
            )
            # Fallback: Scrape environment variables for locale information:
            # Format is typically "en_US.UTF-8" or "en_GB"
            # This isn't really needed with working QLocale option, so commented out.
            # locale_env = os.environ.get("LANG") or os.environ.get("LC_ALL") or ""
            # if locale_env:
            #     # Extract the locale code (e.g., "en_US" from "en_US.UTF-8")
            #     locale_code = locale_env.split(".")[0]
            #     logging.debug(f"Detected locale from environment: {locale_code}")

            #     if locale_code.startswith("en_US"):
            #         return "en_US"
            #     elif locale_code.startswith("en_"):
            #         # en_GB, en_AU, en_CA, etc. all map to en_GB
            #         return default_layout

            return default_layout

    def _close_serial_port(self):
        """
        Utility method to safely close the serial port connection.
        """
        if self.serial_port is not None:
            try:
                self.serial_port.close()
                logging.info("Closed serial port connection")
            except Exception as e:
                logging.error(f"Error closing serial port: {e}")
            self.serial_port = None

    def _populate_video_devices(self):
        """
        Enumerate cameras via QtMultimedia and populate the device menu.

        Enumeration is synchronous on the main thread. QCamera lifecycle
        objects must be created in a thread with a running event loop, and Qt
        spins one in the QApplication main thread, so we don't move this off-
        thread (the legacy background enumerator existed for the pyobjc/comtypes
        probe path that this commit removes).
        """
        self.video_device_var = "Initialising..."
        try:
            cameras = enumerate_cameras()
        except Exception as e:
            logging.error(f"Error discovering video devices: {e}")
            QMessageBox.critical(self, "Error", f"Failed to discover video devices: {e}")
            self.video_devices = []
            self.video_device_var = "Error"
            return

        self.video_devices = cameras
        logging.info(f"Found video devices: {[str(v) for v in cameras]}")

        if cameras:
            # Set the active selection BEFORE populating the menu so the menu
            # builder can render the checkmark on the correct entry.
            self.video_device_var = str(cameras[0])
            self.video_var = 0
            self._populate_video_device_menu()
            # Don't open the camera here — _load_settings (deferred ~10ms after
            # __init_devices) is the single source of truth for opening, so it
            # can apply both the saved device index AND the saved resolution in
            # one _set_camera call. Opening here would cause a visible flicker
            # when the user has a non-default resolution persisted.
            self._populate_resolution_menu(0)
        else:
            self._populate_video_device_menu()
            self.video_device_var = "None found"
            message = (
                "No video devices found.\n\n"
                "Ensure a video capture device is connected and recognised by the system."
                "\n\nIf you have just granted camera permissions, please restart the "
                "application for the changes to take effect."
            )
            QMessageBox.warning(self, "Start-up Warning", message)

    def _on_camera_initialization_error(self, error_msg):
        """
        Callback when camera load/start fails.
        Shows error to user and allows them to select a different camera.
        """
        logging.error(f"Camera initialization error: {error_msg}")
        QMessageBox.critical(
            self,
            "Camera Error",
            f"{error_msg}\n\nPlease select a different camera from the Video menu.",
        )
        self.video_device_var = "Error"

    def _populate_video_device_menu(self):
        """
        Populate the video device dropdown menu with available video devices.
        """
        if self.video_device_menu is None:
            raise TypeError(
                "Initialise video_device_menu before calling _populate_video_device_menu()"
            )

        self.video_device_menu.clear()
        for i, device in enumerate(self.video_devices):
            label = str(device)
            action = QAction(label, self)
            action.setCheckable(True)
            action.triggered.connect(
                lambda _, idx=i, lbl=label: self._on_video_device_selected(idx, lbl)
            )
            self.video_device_menu.addAction(action)

            # Check the current selection
            if i == self.video_var:
                action.setChecked(True)

    def _on_video_device_selected(self, device_idx, device_label):
        """
        Handle selection of a video device.
        """
        if self.video_device_menu is None:
            raise TypeError(
                "Initialise video_device_menu before calling _on_video_device_selected()"
            )

        # Uncheck all other video device actions
        for action in self.video_device_menu.actions():
            action.setChecked(action.text() == device_label)

        self.video_device_var = device_label
        self.video_var = device_idx

        selected_camera = (
            self.video_devices[device_idx] if 0 <= device_idx < len(self.video_devices) else None
        )

        if selected_camera:
            self._set_camera(selected_camera)
            logging.info(
                f"Selected video device: {device_label} "
                f"({selected_camera.width}x{selected_camera.height})"
            )
        else:
            logging.warning(f"Selected video device: {device_label} (no CameraProperties)")

        self._populate_resolution_menu(device_idx)

    def _populate_resolution_menu(self, position: int):
        """
        Populate the resolution menu from the cached CameraProperties at position.

        Reads resolutions from self.video_devices[position].resolutions, populated
        by Qt at enumeration time. The current resolution_var selection is preserved
        when repopulating.
        """
        if self.resolution_menu is None:
            raise TypeError("Initialise resolution_menu before calling _populate_resolution_menu()")

        camera = self.video_devices[position] if 0 <= position < len(self.video_devices) else None
        resolutions = list(camera.resolutions) if camera and camera.resolutions else []
        logging.info(
            "Using %d cached resolutions for device at position %d", len(resolutions), position
        )

        self.resolution_menu.clear()

        use_max_action = QAction("Use Default", self)
        use_max_action.setCheckable(True)
        use_max_action.setChecked(self.resolution_var == "")
        use_max_action.triggered.connect(self._on_use_default_selected)
        self.resolution_menu.addAction(use_max_action)

        if resolutions:
            self.resolution_menu.addSeparator()

        for width, height in resolutions:
            label = f"{width}x{height}"
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(label == self.resolution_var)
            action.triggered.connect(
                lambda checked, w=width, h=height: self._on_resolution_selected(w, h)
            )
            self.resolution_menu.addAction(action)

        # Apply any resolution loaded from settings (or carried over from a prior
        # device selection) now that the menu exists and the camera is ready.
        # If the requested resolution is not supported by this device, fall back
        # to the device default rather than handing QCamera a value it will reject
        # with "Failed to configure preview format" — this happens when the user
        # picks a custom resolution on one camera and then switches to a camera
        # whose viewfinder settings don't include that resolution.
        if self.resolution_var and camera is not None:
            try:
                w, h = (int(x) for x in self.resolution_var.split("x"))
            except (ValueError, IndexError):
                return
            if (w, h) in camera.resolutions:
                self._set_camera(camera, width=w, height=h)
            else:
                logging.info(
                    f"Resolution {self.resolution_var} not supported by {camera.name}; "
                    "falling back to device default"
                )
                self.resolution_var = ""
                for action in self.resolution_menu.actions():
                    action.setChecked(action.text() == "Use Default")

    def _on_use_default_selected(self):
        """
        Clear any explicit resolution override, reverting to the device's default resolution.
        """
        if self.resolution_menu is None:
            raise TypeError("Initialise resolution_menu before calling _on_use_default_selected()")

        for action in self.resolution_menu.actions():
            action.setChecked(action.text() == "Use Default")

        self.resolution_var = ""

        selected_camera = self._selected_camera()
        if selected_camera:
            self._set_camera(selected_camera)
        logging.info("Resolution set to device default")

    def _on_resolution_selected(self, width: int, height: int):
        """
        Handle selection of an explicit capture resolution.
        """
        if self.resolution_menu is None:
            raise TypeError("Initialise resolution_menu before calling _on_resolution_selected()")

        label = f"{width}x{height}"
        for action in self.resolution_menu.actions():
            action.setChecked(action.text() == label)

        self.resolution_var = label
        selected_camera = self._selected_camera()
        if selected_camera:
            self._set_camera(selected_camera, width=width, height=height)
        logging.info(f"Selected resolution: {label}")

    def _on_scale_mode_selected(self, mode: str):
        """
        Switch video scaling mode. "fit" scales the pixmap to fill the view while preserving
        aspect ratio (current default); "1"/"2"/"4" lock the view to an integer pixel ratio
        and show scrollbars/black borders as needed.
        """
        self.scale_mode_var = mode
        for m, action in self._scale_actions.items():
            action.setChecked(m == mode)

        if mode == "fit":
            policy = Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        else:
            policy = Qt.ScrollBarPolicy.ScrollBarAsNeeded
        self.video_view.setHorizontalScrollBarPolicy(policy)
        self.video_view.setVerticalScrollBarPolicy(policy)

        self._apply_scale_mode()
        logging.info(f"Video scale mode set to: {mode}")

    def _apply_scale_mode(self):
        """Apply the currently selected scale mode to the video view's transform."""
        if self.scale_mode_var == "fit":
            self.video_view.resetTransform()
            self.video_view.fitInView(
                self.video_scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio
            )
        else:
            factor = float(self.scale_mode_var)
            self.video_view.resetTransform()
            self.video_view.scale(factor, factor)
        self._sync_native_video_geometry()

    def _sync_native_video_geometry(self) -> None:
        """Align the macOS native preview layer with the transformed video item."""
        if self.native_capture is None or not hasattr(self, "video_view"):
            return
        viewport = self.video_view.viewport()
        if viewport is None:
            return
        polygon = self.video_view.mapFromScene(self.video_scene.sceneRect())
        rect = polygon.boundingRect()
        self.native_capture.set_display_rect(
            rect.x(), rect.y(), rect.width(), rect.height(), viewport.height()
        )

    def _on_resize_window_to_resolution(self):
        """
        Resize the main window so the video view matches the camera resolution scaled
        by the current scale factor. "fit" mode resizes to native (1:1) resolution;
        fixed-ratio modes multiply by their factor (e.g. 2:1 doubles, 1:2 halves).
        """
        if self._is_window_expanded():
            self.showNormal()
            QTimer.singleShot(0, self._resize_window_to_scaled_video)
            return

        self._resize_window_to_scaled_video()

    def _scaled_video_size(self) -> tuple[int, int]:
        """Return the scaled scene size that should fit inside the viewport."""
        camera_width, camera_height = self._camera_resolution()
        factor = 1.0 if self.scale_mode_var == "fit" else float(self.scale_mode_var)
        return (
            int(math.ceil(camera_width * factor)),
            int(math.ceil(camera_height * factor)),
        )

    def _resize_window_to_scaled_video(self):
        """Resize the window so the viewport matches the scaled video size."""
        target_w, target_h = self._scaled_video_size()

        viewport = self.video_view.viewport()
        viewport_w = viewport.width() if viewport is not None else self.video_view.width()
        viewport_h = viewport.height() if viewport is not None else self.video_view.height()

        # Measure the chrome overhead (menubar + statusbar + any margins)
        chrome_w = self.width() - viewport_w
        chrome_h = self.height() - viewport_h

        self.resize(target_w + chrome_w, target_h + chrome_h)
        factor = 1.0 if self.scale_mode_var == "fit" else float(self.scale_mode_var)
        logging.info(
            f"Window resized to {target_w}x{target_h} "
            f"(camera scaled by {factor}x into viewport)"
        )

    def _is_window_expanded(self) -> bool:
        """Return whether the window is fullscreen or maximized."""
        try:
            return self.isFullScreen() or self.isMaximized()
        except RuntimeError:
            return False

    def _on_video_native_size_changed(self, size: QSizeF):
        """
        Update the scene rect and video item size when the camera reports its
        native frame size. QGraphicsVideoItem emits this once the first frame is
        decoded, which is the authoritative resolution (the requested viewfinder
        settings are not always honoured exactly).
        """
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            return
        self.video_item.setSize(size)
        self.video_scene.setSceneRect(self.video_item.boundingRect())
        self._apply_scale_mode()
        logging.debug(f"Video native size: {size.width()}x{size.height()}")

    def resizeEvent(self, event):
        """
        Handle window resize events and re-apply the current scale mode so the
        video item refits the new viewport.
        """
        super().resizeEvent(event)
        if hasattr(self, "video_view"):
            self._apply_scale_mode()

    def _selected_camera(self) -> Optional[CameraProperties]:
        if 0 <= self.video_var < len(self.video_devices):
            return self.video_devices[self.video_var]
        return None

    def _camera_resolution(self) -> tuple[int, int]:
        """Native resolution of the running camera, or default from CameraProperties.

        Prefers QGraphicsVideoItem.nativeSize() (what's actually streaming),
        falls back to the CameraProperties default, and finally to the window
        defaults if no camera is active yet.
        """
        if self.native_capture is not None and self.native_capture.width > 0:
            return self.native_capture.width, self.native_capture.height
        if hasattr(self, "video_item"):
            native = self.video_item.nativeSize()
            if native.isValid() and native.width() > 0 and native.height() > 0:
                return int(native.width()), int(native.height())
        cam = self._selected_camera()
        if cam is not None:
            return cam.width, cam.height
        return self.window_default_width, self.window_default_height

    def _pick_viewfinder_settings(
        self, width: int, height: int
    ) -> Optional[QCameraViewfinderSettings]:
        """Return QCameraViewfinderSettings for width×height with a surface-compatible format.

        QPainterVideoSurface (the backing surface of QGraphicsVideoItem) does not
        support packed YCbCr formats such as UYVY (20) or YUYV (21).  Qt picks the
        first format in supportedViewfinderSettings() — which is almost always UYVY
        for webcams and capture cards — causing "Failed to start viewfinder" and no
        video output.

        This was invisible under the old OpenCV pipeline (removed in d107c08) because
        cv2.VideoCapture decoded every frame to BGR regardless of the camera's native
        format.  The QtMultimedia pipeline streams frames natively between Qt objects,
        so format negotiation between the camera and the surface now matters.

        This method reads the supported format list, picks the highest-priority format
        from _preferred that the camera actually offers at the requested resolution,
        then constructs a fresh QCameraViewfinderSettings with that resolution + format
        (no fps constraint, so Qt chooses the best rate).

        Returns None when no supported settings exist for the given resolution.
        """
        # Formats compatible with QPainterVideoSurface / QGraphicsVideoItem, best first.
        # ARGB32/BGRA32 are universally supported by the software surface.
        # NV12 is listed last — it is hardware-accelerated where supported but
        # QGraphicsVideoItem does not reliably handle it on all platforms.
        _preferred = (
            QVideoFrame.Format_ARGB32,  # 1  — universally supported
            QVideoFrame.Format_BGRA32,  # 8  — universally supported
            QVideoFrame.Format_NV12,  # 22 — hardware path, not reliable on all platforms
        )
        # Formats QGraphicsVideoItem's QPainterVideoSurface cannot render are
        # backend-specific (each platform uses a different Qt backend), so the
        # rejection set is platform-gated. All entries below have a confirmed
        # failure mode on real hardware:
        #   macOS (AVFoundation): UYVY (20) and YUYV (21) both yield "Failed to
        #     start viewfinder" / black screen (Razer capture card).
        #   Windows (DirectShow): Jpeg (30) is a silent black screen on MJPG
        #     capture cards; the surface has no JPEG decoder. YUYV (21) renders
        #     fine here, so it is *not* rejected — letting it through is the
        #     difference between a working 1080p feed and falling back to MJPG.
        #   Linux (V4L2): no failures observed yet — empty set.
        if sys.platform == "darwin":
            _unsupported = {QVideoFrame.Format_UYVY, QVideoFrame.Format_YUYV}
        elif sys.platform == "win32":
            _unsupported = {QVideoFrame.Format_Jpeg}
        else:
            _unsupported = set()

        all_settings = self.qcamera.supportedViewfinderSettings()
        available_fmts = {
            s.pixelFormat()
            for s in all_settings
            if s.resolution().width() == width and s.resolution().height() == height
        }
        logging.debug(
            f"Viewfinder formats available at {width}x{height}: "
            f"{sorted(available_fmts)} (preferred: {list(_preferred)}, "
            f"unsupported: {sorted(_unsupported)})"
        )
        if not available_fmts:
            return None

        for fmt in _preferred:
            if fmt in available_fmts:
                s = QCameraViewfinderSettings()
                s.setResolution(width, height)
                s.setPixelFormat(fmt)
                logging.info(f"Picked preferred viewfinder format {fmt} at {width}x{height}")
                return s

        # No preferred format. Skip known-unrenderable formats; if all that remain
        # are unsupported, fall through to one anyway so the camera still opens —
        # the user sees a black feed plus a clear warning rather than a missing menu.
        fallback = next((f for f in available_fmts if f not in _unsupported), None)
        if fallback is None:
            fallback = next(iter(available_fmts))
            logging.warning(
                f"Camera offers only unrenderable formats {sorted(available_fmts)} at "
                f"{width}x{height}; viewfinder will likely show a black screen. Try a "
                f"different resolution from the Resolution menu."
            )
        else:
            logging.info(
                f"No preferred format at {width}x{height}; using fallback {fallback} "
                f"(available: {sorted(available_fmts)})"
            )
        s = QCameraViewfinderSettings()
        s.setResolution(width, height)
        s.setPixelFormat(fallback)
        return s

    def _set_camera(
        self,
        camera: CameraProperties,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> None:
        """Open `camera` (a CameraProperties) via QCamera and stream into video_item.

        Stops any previously-active QCamera. If width/height are provided, sets
        viewfinder settings to that resolution; otherwise uses the camera default.
        """
        # Tear down any previous camera
        self._stop_native_capture()
        if self.qcamera is not None:
            try:
                self.qcamera.stop()
                self.qcamera.unload()
            except Exception as e:
                logging.debug(f"Error stopping previous QCamera: {e}")
            self.qcamera = None

        if camera.backend == "avfoundation" and sys.platform == "darwin":
            try:
                from kvm_serial.backend.macos_avfoundation import AVFoundationPreviewCapture

                capture = AVFoundationPreviewCapture(camera.unique_id, self.video_view.viewport())
                target_w = width if width is not None else camera.default_resolution[0]
                target_h = height if height is not None else camera.default_resolution[1]
                actual_w, actual_h, actual_fps = capture.start(target_w, target_h)
                self.native_capture = capture
                if self.monitor_hdmi_audio_var:
                    try:
                        capture.set_audio_monitoring(True)
                    except Exception as exc:
                        self.monitor_hdmi_audio_var = False
                        self.hdmi_audio_action.setChecked(False)
                        QTimer.singleShot(
                            0,
                            lambda message=str(exc): QMessageBox.warning(
                                self,
                                "HDMI Audio",
                                "Video started, but HDMI audio monitoring could not be enabled.\n\n"
                                f"{message}",
                            ),
                        )
                self.video_item.setSize(QSizeF(actual_w, actual_h))
                self.video_scene.setSceneRect(self.video_item.boundingRect())
                self._apply_scale_mode()
                logging.info(
                    f"Camera {camera.name} set to native AVFoundation "
                    f"{actual_w}x{actual_h} @ {actual_fps:.3f} fps"
                )
                return
            except Exception as e:
                logging.exception(f"Native AVFoundation capture failed for {camera.name}")
                if camera.info is None:
                    self._on_camera_initialization_error(str(e))
                    return
                logging.warning("Falling back to QtMultimedia for this camera")

        if camera.info is None:
            logging.warning(f"Camera {camera.name} has no QCameraInfo; cannot open")
            return

        self.qcamera = QCamera(camera.info)
        self.qcamera.setViewfinder(self.video_item)

        # Surface camera errors to the user via the existing error path.
        # Bind the camera into the closure so a deferred error from a previously
        # active QCamera can't read errorString() off whatever's in self.qcamera now.
        self.qcamera.error.connect(
            lambda _err, c=self.qcamera: self._on_camera_initialization_error(c.errorString())
        )

        # load() must be called before start() so Qt can negotiate a pixel format
        # compatible with the QGraphicsVideoItem surface.  Without it, the camera
        # starts on its native format (often UYVY for HDMI capture cards) which
        # QGraphicsVideoItem may not support, producing "Failed to start viewfinder".
        self.qcamera.load()

        # Apply viewfinder settings (resolution + pixel format). Must be set
        # before start(). _pick_viewfinder_settings searches the supported list
        # and avoids UYVY when an alternative is available.
        target_w = width if width is not None else camera.default_resolution[0]
        target_h = height if height is not None else camera.default_resolution[1]
        if target_w > 0 and target_h > 0:
            settings = self._pick_viewfinder_settings(target_w, target_h)
            if settings is not None:
                self.qcamera.setViewfinderSettings(settings)
                fmt = settings.pixelFormat()
                logging.info(
                    f"Camera {camera.name} viewfinder set to {target_w}x{target_h} ({fmt})"
                )
            else:
                logging.warning(
                    f"Camera {camera.name}: no supported viewfinder settings for "
                    f"{target_w}x{target_h}; letting Qt choose"
                )

        self.qcamera.start()

    def _stop_native_capture(self) -> None:
        """Stop and release the optional macOS AVFoundation capture session."""
        if self.native_capture is None:
            return
        try:
            self.native_capture.stop()
        except Exception as e:
            logging.debug(f"Error stopping native AVFoundation capture: {e}")
        self.native_capture = None

    def _grab_video_frame(self) -> Optional[QPixmap]:
        """Render the current video item to a QPixmap at native resolution.

        Used by the screenshot path. Falls back to grabbing the view widget if
        the video item has no native size yet (camera hasn't streamed a frame).
        """
        if not hasattr(self, "video_item"):
            return None
        if self.native_capture is not None:
            # QWidget.grab() captures the composited Core Animation preview layer
            # without introducing a per-frame conversion in the live path.
            return self.video_view.grab()
        native = self.video_item.nativeSize()
        if native.isValid() and native.width() > 0:
            pixmap = QPixmap(int(native.width()), int(native.height()))
            pixmap.fill(Qt.GlobalColor.black)
            painter = QPainter(pixmap)
            try:
                self.video_scene.render(
                    painter,
                    target=QRectF(pixmap.rect()),
                    source=self.video_item.boundingRect(),
                )
            finally:
                painter.end()
            return pixmap
        # No native size yet — grab whatever the view is showing.
        return self.video_view.grab()

    def _on_mouse_click(self, x, y, button, down=True):
        """
        Handle mouse button press and release events, logging and triggering mouse operations.
        Args:
            event: QMouseEvent object containing mouse button and position.
        """
        pressed = "pressed" if down else "released"
        logging.info(f"Mouse {self.BUTTON_MAP[button]} {pressed} at {int(x)},{int(y)}")

        if self.mouse_op:
            # Absolute reports already contain the button bitmask. Combining
            # position and button state avoids two back-to-back UART packets
            # (about 27 ms at 9600 baud) for every click. The release coordinate
            # is also the exact final drag position.
            if self.latency_diagnostics_var and self._pending_mouse_move is not None:
                self._latency_mouse_coalesced += 1
            self._pending_mouse_move = None
            self._pending_mouse_event_at = None
            self.mouse_report_timer.stop()
            camera_width, camera_height = self._camera_resolution()
            if camera_width > 0 and camera_height > 0:
                # Releases outside the video rectangle still have to clear the
                # held bit. Clamp them to the nearest edge so a drag cannot
                # leave a button permanently pressed on the target.
                click_x = min(max(int(x), 0), camera_width - 1)
                click_y = min(max(int(y), 0), camera_height - 1)
                started_at = time.monotonic() if self.latency_diagnostics_var else 0.0
                self.mouse_op.on_absolute_click(
                    click_x,
                    click_y,
                    camera_width,
                    camera_height,
                    MouseButton[self.BUTTON_MAP[button]],
                    down,
                )
                if self.latency_diagnostics_var:
                    self._latency_click_dispatch_ms.append((time.monotonic() - started_at) * 1000.0)
                self._last_mouse_report_at = time.monotonic()

    def _on_mouse_move(self, x, y):
        # Store original scene coordinates
        self.pos_x = int(x)
        self.pos_y = int(y)
        self.mouse_var = True

        # Get the native camera resolution
        camera_width, camera_height = self._camera_resolution()

        if 0 > self.pos_x or self.pos_x >= camera_width:
            logging.debug(f"X coordinate out of bounds: 0 <= {x} >= {camera_width}")
            return False
        elif 0 > self.pos_y or self.pos_y >= camera_height:
            logging.debug(f"Y coordinate out of bounds: 0 <= {y} >= {camera_height}")
            return False

        report = f"Mouse: [x:{self.pos_x} y:{self.pos_y}] in [{camera_width}x{camera_height}]"
        logging.debug(report)
        self.status_mouse_label.setText(report)

        # Do not write every Qt mouse event to serial. Keep overwriting this
        # slot; the 50 Hz timer sends only the newest position.
        if self.latency_diagnostics_var:
            self._latency_mouse_events += 1
            if self._pending_mouse_move is not None:
                self._latency_mouse_coalesced += 1
            self._pending_mouse_event_at = time.monotonic()
        self._pending_mouse_move = (
            self.pos_x,
            self.pos_y,
            camera_width,
            camera_height,
        )
        self._schedule_pending_mouse_move()

    def _schedule_pending_mouse_move(self) -> None:
        """Send now when possible, otherwise wait only for UART wire capacity."""
        if self._pending_mouse_move is None or self.mouse_report_timer.isActive():
            return
        baud_rate = int(self.baud_rate_var)
        if baud_rate <= 0:
            baud_rate = 9600
        wire_interval = MOUSE_ABSOLUTE_REPORT_BITS / baud_rate
        elapsed = time.monotonic() - self._last_mouse_report_at
        delay_ms = max(0, math.ceil((wire_interval - elapsed) * 1000))
        self.mouse_report_timer.start(delay_ms)

    def _flush_pending_mouse_move(self) -> None:
        pending = self._pending_mouse_move
        event_at = self._pending_mouse_event_at
        self._pending_mouse_move = None
        self._pending_mouse_event_at = None
        if pending is None:
            return
        started_at = time.monotonic() if self.latency_diagnostics_var else 0.0
        sent = self._send_mouse_position(*pending)
        finished_at = time.monotonic()
        if self.latency_diagnostics_var:
            self._latency_mouse_sent += int(sent)
            if event_at is not None:
                self._latency_mouse_queue_ms.append((started_at - event_at) * 1000.0)
            self._latency_mouse_dispatch_ms.append((finished_at - started_at) * 1000.0)
        if sent:
            self._last_mouse_report_at = finished_at

    def _send_mouse_position(self, x: int, y: int, width: int, height: int) -> bool:
        if not self.mouse_op:
            return False
        try:
            self.mouse_op.on_move(x, y, width, height)
            return True
        except (OverflowError, ValueError) as e:
            logging.error(e)
            logging.error(f"{x}, {y}, {width}, {height}")
            return False

    def _apply_mouse_cursor(self) -> None:
        """Apply the local cursor state to every layer under the video pointer."""
        cursor = Qt.CursorShape.BlankCursor if self.hide_mouse_var else Qt.CursorShape.ArrowCursor
        self.video_view.setCursor(cursor)
        viewport = self.video_view.viewport()
        if viewport is not None:
            viewport.setCursor(cursor)
        if hasattr(self, "video_item"):
            self.video_item.setCursor(cursor)

        self._refresh_native_mouse_cursor()

    def _refresh_native_mouse_cursor(self, force_visible: bool = False) -> None:
        """Keep the native cursor transparent only over the video viewport."""
        if sys.platform != "darwin":
            return
        viewport = self.video_view.viewport()
        if viewport is None:
            return
        hidden = not force_visible and self.hide_mouse_var and self._pointer_over_video
        result = _set_native_macos_video_cursor(viewport, hidden, self._native_mouse_cursor_hidden)
        if result is not None:
            self._native_mouse_cursor_hidden = result

    def _toggle_mouse(self):
        logging.info("Toggling mouse pointer visibility")
        self.hide_mouse_var = not self.hide_mouse_var
        self._apply_mouse_cursor()

    def _toggle_mac_command_as_ctrl(self) -> None:
        """Toggle Mac Command to Windows Control translation for the active KVM."""
        self.mac_command_as_ctrl_var = not self.mac_command_as_ctrl_var
        if self.keyboard_op is not None:
            try:
                self.keyboard_op.set_macos_command_as_ctrl(self.mac_command_as_ctrl_var)
            except Exception as exc:
                logging.warning(f"Could not change keyboard shortcut mode: {exc}")
        logging.info(
            "Mac Command as Windows Ctrl "
            + ("enabled" if self.mac_command_as_ctrl_var else "disabled")
        )

    def _toggle_hdmi_audio(self, checked: bool) -> None:
        """Play the selected capture card's HDMI audio on the Mac."""

        requested = bool(checked)
        if not requested:
            if self.native_capture is not None:
                try:
                    self.native_capture.set_audio_monitoring(False)
                except Exception as exc:
                    logging.warning(f"Could not stop HDMI audio monitoring cleanly: {exc}")
            self.monitor_hdmi_audio_var = False
            self.hdmi_audio_action.setChecked(False)
            return

        if sys.platform != "darwin":
            self.monitor_hdmi_audio_var = False
            self.hdmi_audio_action.setChecked(False)
            QMessageBox.warning(
                self,
                "HDMI Audio",
                "HDMI audio monitoring is currently available only on macOS.",
            )
            return

        if self.native_capture is None:
            self.monitor_hdmi_audio_var = False
            self.hdmi_audio_action.setChecked(False)
            QMessageBox.warning(
                self,
                "HDMI Audio",
                "Select a native AVFoundation video capture device before enabling HDMI audio.",
            )
            return

        try:
            self.native_capture.set_audio_monitoring(True)
        except Exception as exc:
            self.monitor_hdmi_audio_var = False
            self.hdmi_audio_action.setChecked(False)
            QMessageBox.warning(
                self,
                "HDMI Audio",
                "Could not monitor audio from the selected capture device.\n\n"
                f"{exc}\n\n"
                "Check System Settings → Privacy & Security → Microphone and allow "
                "KVM Serial (or Terminal when running from source).",
            )
            return

        self.monitor_hdmi_audio_var = True
        self.hdmi_audio_action.setChecked(True)

    def wheelEvent(self, event: QWheelEvent):
        """
        Handle mouse wheel scroll events and trigger mouse scroll operations.
        Args:
            event: Tkinter event object containing scroll delta and position.
        """
        x = event.x()
        y = event.y()
        dx = event.angleDelta().x()
        dy = event.angleDelta().y()

        logging.info(f"Mouse wheel scroll delta {dx} {dy} at {x}, {y}")

        if self.mouse_op:
            self._flush_pending_mouse_move()
            started_at = time.monotonic() if self.latency_diagnostics_var else 0.0
            self.mouse_op.on_scroll(x, y, dx, dy)
            if self.latency_diagnostics_var:
                self._latency_wheel_dispatch_ms.append((time.monotonic() - started_at) * 1000.0)

        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        """
        Handle KeyPress events, logging and triggering keyboard operations.
        Args:
            event: QKeyEvent event object containing key information.
        """
        logging.debug(f"Key pressed: {event.key()} (0x{event.key():02x})")

        if self.keyboard_op:
            try:
                # parse_key returns True on successful parse
                started_at = time.monotonic() if self.latency_diagnostics_var else 0.0
                self.keyboard_var = self.keyboard_op.parse_key(event)
                if self.latency_diagnostics_var:
                    self._latency_keyboard_dispatch_ms.append(
                        (time.monotonic() - started_at) * 1000.0
                    )
                if (
                    event.type() == QEvent.Type.KeyPress
                    and event.key() >= Qt.Key.Key_Space
                    and event.key() <= Qt.Key.Key_AsciiTilde
                ):
                    self.keyboard_last = "alphanumeric"
                else:
                    self.keyboard_last = "modifier"
            except SerialException as e:
                QMessageBox.critical(self, "Error", f"Error writing to serial port: {e}")
                self._on_quit()

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        """
        Handle KeyRelease events.
        Args:
            event: QKeyEvent event object containing key information.
        """
        logging.debug(f"Key released: {event.key()} (0x{event.key():02x})")

        try:
            if self.keyboard_op:
                started_at = time.monotonic() if self.latency_diagnostics_var else 0.0
                self.keyboard_op.parse_key(event)
                if self.latency_diagnostics_var:
                    self._latency_keyboard_dispatch_ms.append(
                        (time.monotonic() - started_at) * 1000.0
                    )
        except SerialException as e:
            QMessageBox.critical(self, "Error", f"Error writing to serial port: {e}")
            self._on_quit()

        super().keyReleaseEvent(event)

    def _get_version(self):
        import toml

        try:
            pyproject_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "pyproject.toml"
            )
            with open(pyproject_path, "r") as f:
                data = toml.load(f)
            return data["project"]["version"]
        except (FileNotFoundError, KeyError, AttributeError, ValueError) as e:
            logging.warning(f"Could not read version from pyproject.toml: {e}")
            return "?"

    def _show_about(self):
        version = self._get_version()
        QMessageBox.about(
            self,
            "About Serial KVM",
            f"<p><b>Serial KVM</b><br/>Version {version}</p>\n"
            "<p>Keyboard/Mouse over Serial using CH9329.<p>\n"
            "<p>(c) 2024-2025 Samantha Finnigan <a href='https://github.com/sjmf'>@sjmf</a> and contributors.</p>"
            "<p>Available under <a href='https://github.com/sjmf/kvm-serial/blob/main/LICENSE.md'>"
            "MIT License</a></p>",
        )

    def _send_ctrl_alt_del(self):
        """Send CTRL+ALT+DEL key combination"""
        if not self.keyboard_op:
            logging.warning("No keyboard operation available")
            return

        try:
            # On macOS, Qt maps Cmd to Control and Ctrl to Meta
            # We want to send actual Control key, so we use Meta on macOS
            ctrl_key = Qt.Key.Key_Control
            if sys.platform == "darwin":
                ctrl_key = Qt.Key.Key_Meta

            # Create synthetic key events
            ctrl_alt_del = [ctrl_key, Qt.Key.Key_Alt, Qt.Key.Key_Delete]

            # Press and release all keys
            for action in [QEvent.Type.KeyPress, QEvent.Type.KeyRelease]:
                for key in ctrl_alt_del:
                    self.keyboard_op.parse_key(
                        QKeyEvent(action, key, Qt.KeyboardModifier.NoModifier)
                    )

            logging.info("Sent CTRL+ALT+DEL")
        except Exception as e:
            logging.error(f"Error sending CTRL+ALT+DEL: {e}")

    def _on_paste(self):
        """Paste text from clipboard to remote machine, transmitting char-wise"""
        if not self.keyboard_op:
            logging.warning("No keyboard operation available")
            return

        try:
            clipboard = QApplication.clipboard()
            if clipboard is None:
                logging.warning("Could not access clipboard")
                return

            paste_text = clipboard.text()
            if not paste_text:
                logging.info("Clipboard is empty")
                return

            # Convert string to scancodes with key-up signals between characters
            scancodes = string_to_scancodes(paste_text, key_repeat=1, key_up=1)

            # Disable paste action while transmitting
            self.paste_action.setEnabled(False)

            # Start transmitting scancodes asynchronously
            self._send_next_scancode(scancodes, 0, len(paste_text))
        except Exception as e:
            logging.error(f"Error pasting from clipboard: {e}")
            self.paste_action.setEnabled(True)

    def _send_next_scancode(self, scancodes: list, index: int, char_count: int):
        """Send the next scancode in the paste buffer, scheduling the next one via QTimer"""
        if index >= len(scancodes):
            logging.info(f"Pasted {char_count} characters")
            self.paste_action.setEnabled(True)
            return

        try:
            scancode = scancodes[index]
            char_repr = scancode_to_ascii(scancode) or "?"
            logging.debug(
                f"Paste [{index}]: {char_repr!r} -> ({', '.join(hex(b) for b in scancode)})"
            )
            self.keyboard_op.hid_serial_out.send_scancode(bytes(scancode))  # type: ignore
        except Exception as e:
            logging.error(f"Error during paste at index {index}: {e}")
            self.paste_action.setEnabled(True)
            return

        # Schedule the next scancode after 10ms delay:
        # I'm tracking `index` here - arguably we could use a deque for scancodes, and
        #  do .popleft() instead. I've implemented it this way out of performance concerns,
        #  plus, it's more debuggable if we don't mutate state every time we hit the function.
        QTimer.singleShot(10, lambda: self._send_next_scancode(scancodes, index + 1, char_count))

    def closeEvent(self, event):
        """Clean up resources when closing the application"""
        # The window close button reaches this path without _on_quit().  Mark the
        # shutdown before Qt emits its subsequent focus/leave events.
        self._quitting = True

        # Prevent timer callbacks from touching Qt/AppKit objects while Cocoa is
        # tearing down the native window.
        for timer_name in ("cursor_refresh_timer", "mouse_report_timer", "status_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                try:
                    timer.stop()
                except RuntimeError:
                    pass

        try:
            self._refresh_native_mouse_cursor(force_visible=True)
        except (RuntimeError, AttributeError) as exc:
            logging.debug(f"Native cursor was already unavailable during shutdown: {exc}")

        # Release modifiers before closing the serial transport.  A later
        # focusOutEvent deliberately skips this work once _quitting is set.
        self._release_keyboard_capture()

        # Stop and tear down the active QCamera (QtMultimedia owns the threading
        # internally, so no manual quit/wait is needed)
        self._stop_native_capture()
        if self.qcamera is not None:
            try:
                self.qcamera.stop()
                self.qcamera.unload()
            except Exception as e:
                logging.debug(f"Error stopping QCamera on close: {e}")
            self.qcamera = None

        # Stop the DataCommManager (background threads, descriptor handshake
        # for CH9350 state 0/1) before closing the underlying port.
        self._stop_comm_manager()

        # Close serial port if open
        self._close_serial_port()
        self.keyboard_op = None
        self.mouse_op = None

        event.accept()

    def _on_quit(self) -> None:
        self._quitting = True
        self.close()


def _resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller.

    PyInstaller onefile builds extract bundled data files to a temporary
    directory and expose its path via sys._MEIPASS. When running from source,
    resolve relative to the project root instead.
    """
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", relative_path)


def main():
    """
    Entry point for the application. Configures logging and shows the KVMQtGui main window.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = QApplication(sys.argv)

    # Set application icon (used for title bar and taskbar)
    icon_path = _resource_path(os.path.join("assets", "icon.png"))
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    window = KVMQtGui()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
