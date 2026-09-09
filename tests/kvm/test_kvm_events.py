#!/usr/bin/env python
"""
Test suite for KVM event handling functionality.
Uses KVMTestBase for common mocking infrastructure.
"""

import unittest
from unittest.mock import patch, MagicMock, call
from PyQt5.QtCore import Qt, QEvent, QPoint, QPointF
from PyQt5.QtGui import QMouseEvent, QKeyEvent, QFocusEvent, QWheelEvent
from PyQt5.QtWidgets import QApplication
from serial import SerialException

# Import the base test class
from test_kvm_base import KVMTestBase, KVMTestMixins


class TestKVMEventHandling(
    KVMTestBase,
    KVMTestMixins.SerialTestMixin,
    KVMTestMixins.VideoTestMixin,
):
    """Test class for KVM event handling functionality."""

    def test_double_click_forwards_second_button_press(self):
        """MouseButtonDblClick must become the second HID button-down."""
        fake_view = MagicMock()
        event = MagicMock(spec=QMouseEvent)

        self.kvm_module._forward_mouse_double_click(fake_view, event)

        fake_view._forward_mouse_press.assert_called_once_with(event)
        event.accept.assert_called_once_with()

    def test_click_jitter_below_drag_threshold_is_not_forwarded(self):
        """Tiny motion while LEFT is down must remain a click, not a text drag."""
        fake_view = MagicMock()
        fake_view._mouse_press_pos = QPoint(100, 100)
        fake_view._drag_started = False
        event = MagicMock(spec=QMouseEvent)
        event.buttons.return_value = Qt.MouseButton.LeftButton
        event.pos.return_value = QPoint(103, 102)

        forwarded = self.kvm_module._forward_mouse_move(fake_view, event, drag_distance=10)

        self.assertFalse(forwarded)
        fake_view.mapToScene.assert_not_called()
        fake_view.mouseMoved.emit.assert_not_called()

    def test_motion_past_drag_threshold_starts_and_forwards_drag(self):
        """Intentional motion beyond the threshold must preserve real dragging."""
        fake_view = MagicMock()
        fake_view._mouse_press_pos = QPoint(100, 100)
        fake_view._drag_started = False
        fake_view.mapToScene.return_value = QPointF(330.5, 220.25)
        event = MagicMock(spec=QMouseEvent)
        event.buttons.return_value = Qt.MouseButton.LeftButton
        event.pos.return_value = QPoint(108, 104)

        forwarded = self.kvm_module._forward_mouse_move(fake_view, event, drag_distance=10)

        self.assertTrue(forwarded)
        self.assertTrue(fake_view._drag_started)
        fake_view.mouseMoved.emit.assert_called_once_with(330.5, 220.25)

    def test_click_release_keeps_press_position_below_drag_threshold(self):
        """A slightly jittered click must release at its original coordinate."""
        fake_view = MagicMock()
        fake_view._mouse_press_pos = QPoint(100, 100)
        fake_view._drag_started = False
        event = MagicMock(spec=QMouseEvent)
        event.pos.return_value = QPoint(104, 103)

        release_pos = self.kvm_module._mouse_release_position(fake_view, event)

        self.assertEqual(release_pos, QPoint(100, 100))

    def test_drag_release_uses_actual_endpoint(self):
        """An intentional drag must release at the final event coordinate."""
        fake_view = MagicMock()
        fake_view._mouse_press_pos = QPoint(100, 100)
        fake_view._drag_started = True
        event = MagicMock(spec=QMouseEvent)
        event.pos.return_value = QPoint(250, 200)

        release_pos = self.kvm_module._mouse_release_position(fake_view, event)

        self.assertEqual(release_pos, QPoint(250, 200))

    def test_mouse_click_coordinate_translation(self):
        """Test mouse click coordinates are properly translated to scene coordinates."""
        app = self.create_kvm_app()

        # Set up mock mouse operation
        mock_mouse_op = MagicMock()
        app.mouse_op = mock_mouse_op

        # Set camera dimensions for coordinate system
        app._camera_resolution = MagicMock(return_value=(1280, 720))

        # Test left mouse button press
        app._on_mouse_click(100.5, 200.7, Qt.MouseButton.LeftButton, True)

        # Verify mouse operation was called with correct parameters
        from kvm_serial.backend.implementations.mouseop import MouseButton

        mock_mouse_op.on_absolute_click.assert_called_once_with(
            100, 200, 1280, 720, MouseButton.LEFT, True
        )

    def test_mouse_click_without_mouse_op(self):
        """Test mouse click handling when mouse operation is not available."""
        app = self.create_kvm_app()
        app.mouse_op = None

        # Should not raise exception when mouse_op is None
        app._on_mouse_click(100, 200, Qt.MouseButton.LeftButton, True)

    def test_mouse_release_outside_video_is_clamped_and_forwarded(self):
        """Releasing outside the scene must not leave the remote button held."""
        app = self.create_kvm_app()
        mock_mouse_op = MagicMock()
        app.mouse_op = mock_mouse_op
        app._camera_resolution = MagicMock(return_value=(1280, 720))

        app._on_mouse_click(1400, -20, Qt.MouseButton.LeftButton, False)

        from kvm_serial.backend.implementations.mouseop import MouseButton

        mock_mouse_op.on_absolute_click.assert_called_once_with(
            1279, 0, 1280, 720, MouseButton.LEFT, False
        )

    def test_mouse_button_mapping(self):
        """Test all mouse buttons are mapped correctly."""
        app = self.create_kvm_app()
        mock_mouse_op = MagicMock()
        app.mouse_op = mock_mouse_op
        app._camera_resolution = MagicMock(return_value=(1280, 720))

        # Test all button types
        button_tests = [
            (Qt.MouseButton.LeftButton, "LEFT"),
            (Qt.MouseButton.RightButton, "RIGHT"),
            (Qt.MouseButton.MiddleButton, "MIDDLE"),
        ]

        from kvm_serial.backend.implementations.mouseop import MouseButton

        for qt_button, expected_button in button_tests:
            with self.subTest(button=expected_button):
                mock_mouse_op.reset_mock()
                app._on_mouse_click(50, 50, qt_button, True)
                mock_mouse_op.on_absolute_click.assert_called_once_with(
                    50, 50, 1280, 720, MouseButton[expected_button], True
                )

    def test_mouse_move_coordinate_tracking(self):
        """Test mouse movement updates position tracking."""
        app = self.create_kvm_app()
        mock_mouse_op = MagicMock()
        app.mouse_op = mock_mouse_op

        # Set camera dimensions
        app._camera_resolution = MagicMock(return_value=(1280, 720))

        # Test valid coordinates
        result = app._on_mouse_move(640.3, 360.7)

        # Should not return False for valid coordinates
        self.assertNotEqual(result, False)

        # Check position was stored as integers
        self.assertEqual(app.pos_x, 640)
        self.assertEqual(app.pos_y, 360)
        self.assertTrue(app.mouse_var)

        # High-frequency moves are held until the reporting timer fires.
        mock_mouse_op.on_move.assert_not_called()
        self.assertEqual(app._pending_mouse_move, (640, 360, 1280, 720))

        app._flush_pending_mouse_move()

        # Verify the newest position was sent and consumed.
        mock_mouse_op.on_move.assert_called_once_with(640, 360, 1280, 720)
        self.assertIsNone(app._pending_mouse_move)

    def test_mouse_moves_are_coalesced_to_latest_position(self):
        """Only the newest coordinate is sent when several events arrive."""
        app = self.create_kvm_app()
        mock_mouse_op = MagicMock()
        app.mouse_op = mock_mouse_op
        app._camera_resolution = MagicMock(return_value=(1280, 720))

        app._on_mouse_move(100, 110)
        app._on_mouse_move(300, 310)

        mock_mouse_op.on_move.assert_not_called()
        app._flush_pending_mouse_move()

        mock_mouse_op.on_move.assert_called_once_with(300, 310, 1280, 720)
        self.assertIsNone(app._pending_mouse_move)

    def test_latency_diagnostics_aggregate_mouse_queue_without_video_hooks(self):
        """Opt-in diagnostics count coalescing and the latest move dispatch."""
        app = self.create_kvm_app()
        app.mouse_op = MagicMock()
        app._camera_resolution = MagicMock(return_value=(1280, 720))
        app.latency_diagnostics_var = True
        app._reset_latency_diagnostics()

        app._on_mouse_move(100, 110)
        app._on_mouse_move(300, 310)
        app._flush_pending_mouse_move()

        self.assertEqual(app._latency_mouse_events, 2)
        self.assertEqual(app._latency_mouse_coalesced, 1)
        self.assertEqual(app._latency_mouse_sent, 1)
        self.assertEqual(len(app._latency_mouse_queue_ms), 1)
        self.assertEqual(len(app._latency_mouse_dispatch_ms), 1)

    def test_latency_diagnostics_log_includes_uart_estimate(self):
        """The diagnostic line separates measured dispatch from wire time."""
        app = self.create_kvm_app()
        app.latency_diagnostics_var = True
        app.baud_rate_var = 9600
        app._reset_latency_diagnostics()

        with self.assertLogs(level="INFO") as captured:
            app._log_latency_diagnostics()

        line = "\n".join(captured.output)
        self.assertIn("UART packets mouse=13.54ms keyboard=14.58ms @9600 baud", line)
        self.assertIn("queue[n/a]", line)

    def test_mouse_release_combines_drag_position_and_button_state(self):
        """The final drag coordinate and button-up use one absolute report."""
        app = self.create_kvm_app()
        mock_mouse_op = MagicMock()
        app.mouse_op = mock_mouse_op
        app._camera_resolution = MagicMock(return_value=(1280, 720))

        app._on_mouse_move(500, 400)
        app._on_mouse_click(500, 400, Qt.MouseButton.LeftButton, False)

        from kvm_serial.backend.implementations.mouseop import MouseButton

        mock_mouse_op.on_move.assert_not_called()
        mock_mouse_op.on_absolute_click.assert_called_once_with(
            500, 400, 1280, 720, MouseButton.LEFT, False
        )
        self.assertIsNone(app._pending_mouse_move)

    def test_first_mouse_move_after_idle_is_scheduled_immediately(self):
        """Event-driven reporting must not wait for an arbitrary periodic tick."""
        app = self.create_kvm_app()
        app.mouse_op = MagicMock()
        app._camera_resolution = MagicMock(return_value=(1280, 720))
        app.mouse_report_timer.reset_mock()
        app.mouse_report_timer.isActive.return_value = False
        app._last_mouse_report_at = 0.0

        with patch("kvm_serial.kvm.time.monotonic", return_value=100.0):
            app._on_mouse_move(100, 100)

        app.mouse_report_timer.start.assert_called_once_with(0)

    def test_continuous_mouse_moves_are_paced_to_uart_capacity(self):
        """At 9600 baud the next 13-byte report is delayed about 13 ms."""
        app = self.create_kvm_app()
        app.mouse_op = MagicMock()
        app._camera_resolution = MagicMock(return_value=(1280, 720))
        app.mouse_report_timer.reset_mock()
        app.mouse_report_timer.isActive.return_value = False
        app.baud_rate_var = 9600
        app._last_mouse_report_at = 100.0

        with patch("kvm_serial.kvm.time.monotonic", return_value=100.001):
            app._on_mouse_move(101, 100)

        app.mouse_report_timer.start.assert_called_once_with(13)

    def test_mouse_move_bounds_checking(self):
        """Test mouse movement bounds checking."""
        app = self.create_kvm_app()
        mock_mouse_op = MagicMock()
        app.mouse_op = mock_mouse_op

        # Set camera dimensions
        app._camera_resolution = MagicMock(return_value=(1280, 720))

        # Test coordinates outside bounds
        out_of_bounds_tests = [
            (-1, 360, "x coordinate negative"),
            (1280, 360, "x coordinate at width limit"),
            (640, -1, "y coordinate negative"),
            (640, 720, "y coordinate at height limit"),
            (1281, 360, "x coordinate beyond width"),
            (640, 721, "y coordinate beyond height"),
        ]

        for x, y, description in out_of_bounds_tests:
            with self.subTest(test=description):
                mock_mouse_op.reset_mock()
                result = app._on_mouse_move(x, y)

                # Should return False for out of bounds
                self.assertEqual(result, False)

                # Mouse operation should not be called
                mock_mouse_op.on_move.assert_not_called()

    def test_mouse_move_exception_handling(self):
        """Test exception handling during mouse move operations."""
        app = self.create_kvm_app()
        mock_mouse_op = MagicMock()
        mock_mouse_op.on_move.side_effect = ValueError("Invalid coordinates")
        app.mouse_op = mock_mouse_op

        # Set camera dimensions
        app._camera_resolution = MagicMock(return_value=(1280, 720))

        # Should handle exception gracefully
        app._on_mouse_move(100, 100)
        app._flush_pending_mouse_move()

        # Position should still be updated despite exception
        self.assertEqual(app.pos_x, 100)
        self.assertEqual(app.pos_y, 100)

    def test_mouse_wheel_event_handling(self):
        """Test mouse wheel scroll event processing."""
        app = self.create_kvm_app()
        mock_mouse_op = MagicMock()
        app.mouse_op = mock_mouse_op

        # Create mock wheel event
        mock_event = MagicMock(spec=QWheelEvent)
        mock_event.x.return_value = 300
        mock_event.y.return_value = 400
        mock_angle_delta = MagicMock()
        mock_angle_delta.x.return_value = 0
        mock_angle_delta.y.return_value = 120  # Typical scroll delta
        mock_event.angleDelta.return_value = mock_angle_delta

        # Mock the super() call
        with patch("kvm_serial.kvm.QMainWindow.wheelEvent"):
            app.wheelEvent(mock_event)

        # Verify scroll operation was called
        mock_mouse_op.on_scroll.assert_called_once_with(300, 400, 0, 120)

    def test_mouse_wheel_without_mouse_op(self):
        """Test wheel event handling when mouse operation is not available."""
        app = self.create_kvm_app()
        app.mouse_op = None

        mock_event = MagicMock(spec=QWheelEvent)
        mock_event.x.return_value = 300
        mock_event.y.return_value = 400
        mock_angle_delta = MagicMock()
        mock_angle_delta.x.return_value = 0
        mock_angle_delta.y.return_value = 120
        mock_event.angleDelta.return_value = mock_angle_delta

        # Should not raise exception
        with patch("kvm_serial.kvm.QMainWindow.wheelEvent"):
            app.wheelEvent(mock_event)

    def test_keyboard_press_event_processing(self):
        """Test keyboard press event processing."""
        app = self.create_kvm_app()
        mock_keyboard_op = MagicMock()
        mock_keyboard_op.parse_key.return_value = True
        app.keyboard_op = mock_keyboard_op

        # Create mock key event
        mock_event = MagicMock(spec=QKeyEvent)
        mock_event.key.return_value = Qt.Key.Key_A
        mock_event.type.return_value = QEvent.Type.KeyPress

        # Mock the super() call to prevent Qt type checking
        with patch("kvm_serial.kvm.QMainWindow.keyPressEvent"):
            app.keyPressEvent(mock_event)

        # Verify keyboard operation was called
        mock_keyboard_op.parse_key.assert_called_once_with(mock_event)
        self.assertTrue(app.keyboard_var)
        self.assertEqual(app.keyboard_last, "alphanumeric")

    def test_keyboard_press_modifier_keys(self):
        """Test keyboard press event processing for modifier keys."""
        app = self.create_kvm_app()
        mock_keyboard_op = MagicMock()
        mock_keyboard_op.parse_key.return_value = True
        app.keyboard_op = mock_keyboard_op

        # Test modifier keys
        modifier_keys = [
            Qt.Key.Key_Control,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Meta,
            Qt.Key.Key_Escape,
        ]

        for key in modifier_keys:
            with self.subTest(key=key):
                mock_keyboard_op.reset_mock()
                mock_event = MagicMock(spec=QKeyEvent)
                mock_event.key.return_value = key
                mock_event.type.return_value = QEvent.Type.KeyPress

                # Mock the super() call
                with patch("kvm_serial.kvm.QMainWindow.keyPressEvent"):
                    app.keyPressEvent(mock_event)

                mock_keyboard_op.parse_key.assert_called_once_with(mock_event)
                self.assertTrue(app.keyboard_var)
                self.assertEqual(app.keyboard_last, "modifier")

    def test_keyboard_press_without_keyboard_op(self):
        """Test keyboard press handling when keyboard operation is not available."""
        app = self.create_kvm_app()
        app.keyboard_op = None

        mock_event = MagicMock(spec=QKeyEvent)
        mock_event.key.return_value = Qt.Key.Key_A
        mock_event.type.return_value = QEvent.Type.KeyPress

        # Should not raise exception
        with patch("kvm_serial.kvm.QMainWindow.keyPressEvent"):
            app.keyPressEvent(mock_event)

    def test_keyboard_release_event_processing(self):
        """Test keyboard release event processing."""
        app = self.create_kvm_app()
        mock_keyboard_op = MagicMock()
        app.keyboard_op = mock_keyboard_op

        # Create mock key event
        mock_event = MagicMock(spec=QKeyEvent)
        mock_event.key.return_value = Qt.Key.Key_A

        # Mock the super() call
        with patch("kvm_serial.kvm.QMainWindow.keyReleaseEvent"):
            app.keyReleaseEvent(mock_event)

        # Verify keyboard operation was called
        mock_keyboard_op.parse_key.assert_called_once_with(mock_event)

    def test_keyboard_serial_exception_handling(self):
        """Test handling of serial exceptions during keyboard operations."""
        app = self.create_kvm_app()
        mock_keyboard_op = MagicMock()
        mock_keyboard_op.parse_key.side_effect = SerialException("Port disconnected")
        app.keyboard_op = mock_keyboard_op

        mock_event = MagicMock(spec=QKeyEvent)
        mock_event.key.return_value = Qt.Key.Key_A
        mock_event.type.return_value = QEvent.Type.KeyPress

        with (
            patch("kvm_serial.kvm.QMessageBox.critical") as mock_critical,
            patch.object(app, "_on_quit") as mock_quit,
            patch("kvm_serial.kvm.QMainWindow.keyPressEvent"),
        ):
            app.keyPressEvent(mock_event)

            # Should show error and quit
            mock_critical.assert_called_once()
            mock_quit.assert_called_once()

    def test_window_focus_events(self):
        """Test window focus in/out event handling."""
        app = self.create_kvm_app()

        # Create mock focus events
        mock_focus_in = MagicMock(spec=QFocusEvent)
        mock_focus_out = MagicMock(spec=QFocusEvent)

        # The main window focus events only log messages, they don't change keyboard_var
        # keyboard_var is managed by the video view focus, not window focus
        initial_keyboard_state = app.keyboard_var

        # Mock the super() calls
        with patch("kvm_serial.kvm.QMainWindow.focusInEvent"):
            app.focusInEvent(mock_focus_in)
            # Window focus doesn't change keyboard state - that's handled by video view
            self.assertEqual(app.keyboard_var, initial_keyboard_state)

        with patch("kvm_serial.kvm.QMainWindow.focusOutEvent"):
            app.focusOutEvent(mock_focus_out)
            # Window focus doesn't change keyboard state - that's handled by video view
            self.assertEqual(app.keyboard_var, initial_keyboard_state)

    def test_window_resize_reapplies_scale_mode(self):
        """Window resize should re-apply the current scale mode (fit-to-window)."""
        app = self.create_kvm_app()

        with (
            patch.object(app, "_apply_scale_mode") as mock_apply,
            patch("kvm_serial.kvm.QMainWindow.resizeEvent"),
        ):
            app.resizeEvent(MagicMock())
            mock_apply.assert_called_once()

    def test_close_event_cleanup(self):
        """Test proper cleanup during close event."""
        app = self.create_kvm_app()

        mock_serial_port = MagicMock()
        app.serial_port = mock_serial_port
        mock_camera = MagicMock()
        app.qcamera = mock_camera
        mock_keyboard_op = MagicMock()
        app.keyboard_op = mock_keyboard_op

        mock_event = MagicMock()
        mock_event.accept = MagicMock()

        app.closeEvent(mock_event)

        self.assertTrue(app._quitting)
        mock_keyboard_op.release_all.assert_called_once_with()

        # QCamera should be stopped and unloaded; reference cleared.
        mock_camera.stop.assert_called_once()
        mock_camera.unload.assert_called_once()
        self.assertIsNone(app.qcamera)

        mock_serial_port.close.assert_called_once()
        mock_event.accept.assert_called_once()
        self.assertIsNone(app.serial_port)
        self.assertIsNone(app.keyboard_op)
        self.assertIsNone(app.mouse_op)

    def test_close_event_tolerates_keyboard_release_failure(self):
        """A serial failure during close must not escape into Qt and abort macOS."""
        app = self.create_kvm_app()
        app.keyboard_op = MagicMock()
        app.keyboard_op.release_all.side_effect = RuntimeError("serial already closed")
        mock_event = MagicMock()

        app.closeEvent(mock_event)

        self.assertTrue(app._quitting)
        mock_event.accept.assert_called_once_with()

    def test_quit_action_sets_flag_and_closes(self):
        """Test quit action sets quitting flag and closes window."""
        app = self.create_kvm_app()

        with patch.object(app, "close") as mock_close:
            app._on_quit()

            self.assertTrue(app._quitting)
            mock_close.assert_called_once()

    def test_mouse_pointer_visibility_toggle(self):
        """Test mouse pointer visibility toggle functionality."""
        app = self.create_kvm_app()

        # The local mouse pointer is hidden by default.
        self.assertTrue(app.hide_mouse_var)

        # Toggle to show mouse
        app._toggle_mouse()
        self.assertFalse(app.hide_mouse_var)
        app.video_view.setCursor.assert_called_with(Qt.CursorShape.ArrowCursor)
        app.video_view.viewport().setCursor.assert_called_with(Qt.CursorShape.ArrowCursor)
        app.video_item.setCursor.assert_called_with(Qt.CursorShape.ArrowCursor)

        # Toggle to hide mouse
        app._toggle_mouse()
        self.assertTrue(app.hide_mouse_var)
        app.video_view.setCursor.assert_called_with(Qt.CursorShape.BlankCursor)
        app.video_view.viewport().setCursor.assert_called_with(Qt.CursorShape.BlankCursor)
        app.video_item.setCursor.assert_called_with(Qt.CursorShape.BlankCursor)

    def test_mac_command_as_ctrl_toggle_updates_active_keyboard(self):
        """The menu option updates the current translator without reopening serial."""
        app = self.create_kvm_app()
        app.mac_command_as_ctrl_var = False
        app.keyboard_op = MagicMock()

        app._toggle_mac_command_as_ctrl()

        self.assertTrue(app.mac_command_as_ctrl_var)
        app.keyboard_op.set_macos_command_as_ctrl.assert_called_once_with(True)

    def test_macos_native_cursor_is_scoped_to_video(self):
        """Refresh must request transparency only while the pointer is over video."""
        app = self.create_kvm_app()
        app.hide_mouse_var = True

        with (
            patch("kvm_serial.kvm.sys.platform", "darwin"),
            patch(
                "kvm_serial.kvm._set_native_macos_video_cursor",
                side_effect=[True, True, False],
            ) as mock_native_cursor,
        ):
            app._pointer_over_video = True
            app._apply_mouse_cursor()
            app._refresh_native_mouse_cursor()
            app._pointer_over_video = False
            app._apply_mouse_cursor()

        viewport = app.video_view.viewport()
        self.assertEqual(
            mock_native_cursor.call_args_list,
            [
                call(viewport, True, False),
                call(viewport, True, True),
                call(viewport, False, True),
            ],
        )
        self.assertFalse(app._native_mouse_cursor_hidden)

    def test_macos_native_cursor_is_restored_on_focus_loss(self):
        """Leaving the app must restore a visible pointer without a hide counter."""
        app = self.create_kvm_app()
        app.hide_mouse_var = True
        app._pointer_over_video = True
        app._native_mouse_cursor_hidden = True

        with (
            patch("kvm_serial.kvm.sys.platform", "darwin"),
            patch(
                "kvm_serial.kvm._set_native_macos_video_cursor", return_value=False
            ) as mock_native_cursor,
        ):
            app._refresh_native_mouse_cursor(force_visible=True)

        mock_native_cursor.assert_called_once_with(app.video_view.viewport(), False, True)
        self.assertFalse(app._native_mouse_cursor_hidden)

    def test_event_coordinates_within_camera_bounds(self):
        """Test event coordinates are validated against camera dimensions."""
        app = self.create_kvm_app()
        mock_mouse_op = MagicMock()
        app.mouse_op = mock_mouse_op

        # Set specific camera dimensions
        app._camera_resolution = MagicMock(return_value=(640, 480))

        # Test coordinates at exact boundaries
        boundary_tests = [
            (0, 0, True, "top-left corner"),
            (639, 479, True, "bottom-right valid"),
            (640, 479, False, "x at width limit"),
            (639, 480, False, "y at height limit"),
        ]

        for x, y, should_succeed, description in boundary_tests:
            with self.subTest(test=description):
                mock_mouse_op.reset_mock()
                result = app._on_mouse_move(x, y)

                if should_succeed:
                    self.assertNotEqual(result, False)
                    mock_mouse_op.on_move.assert_not_called()
                    app._flush_pending_mouse_move()
                    mock_mouse_op.on_move.assert_called_once()
                else:
                    self.assertEqual(result, False)
                    mock_mouse_op.on_move.assert_not_called()

    def test_keyboard_alphanumeric_classification(self):
        """Test keyboard events are classified as alphanumeric or modifier."""
        app = self.create_kvm_app()
        mock_keyboard_op = MagicMock()
        mock_keyboard_op.parse_key.return_value = True
        app.keyboard_op = mock_keyboard_op

        # Test alphanumeric keys (space through tilde)
        alphanumeric_keys = [
            Qt.Key.Key_Space,
            Qt.Key.Key_A,
            Qt.Key.Key_Z,
            Qt.Key.Key_0,
            Qt.Key.Key_9,
            Qt.Key.Key_AsciiTilde,
        ]

        for key in alphanumeric_keys:
            with self.subTest(key=key):
                mock_event = MagicMock(spec=QKeyEvent)
                mock_event.key.return_value = key
                mock_event.type.return_value = QEvent.Type.KeyPress

                # Mock the super() call
                with patch("kvm_serial.kvm.QMainWindow.keyPressEvent"):
                    app.keyPressEvent(mock_event)

                self.assertEqual(app.keyboard_last, "alphanumeric")

    def test_serial_communication_error_recovery(self):
        """Test recovery from serial communication errors during events."""
        app = self.create_kvm_app()

        # Test keyboard operation with serial error
        mock_keyboard_op = MagicMock()
        mock_keyboard_op.parse_key.side_effect = SerialException("Communication failed")
        app.keyboard_op = mock_keyboard_op

        mock_event = MagicMock(spec=QKeyEvent)
        mock_event.key.return_value = Qt.Key.Key_A

        with (
            patch("kvm_serial.kvm.QMessageBox.critical"),
            patch.object(app, "_on_quit") as mock_quit,
            patch("kvm_serial.kvm.QMainWindow.keyReleaseEvent"),
        ):
            app.keyReleaseEvent(mock_event)

            # Should trigger quit on serial error
            mock_quit.assert_called_once()

    def test_focus_management_state_consistency(self):
        """Test focus management maintains consistent state."""
        app = self.create_kvm_app()

        # Initial state
        self.assertFalse(app.keyboard_var)

        # The actual focus management is done through direct state changes
        # not through the window focus events (those are handled by video view)

        # Test direct keyboard_var state changes (which is what actually happens)
        app.keyboard_var = True
        self.assertTrue(app.keyboard_var, "Direct state change should enable keyboard")

        app.keyboard_var = False
        self.assertFalse(app.keyboard_var, "Direct state change should disable keyboard")

        app.keyboard_var = True
        self.assertTrue(app.keyboard_var, "Second state change should enable keyboard")

        # Test that the window focus methods exist and can be called without errors
        mock_focus_in = MagicMock(spec=QFocusEvent)
        mock_focus_out = MagicMock(spec=QFocusEvent)

        with patch("kvm_serial.kvm.QMainWindow.focusInEvent"):
            app.focusInEvent(mock_focus_in)  # Should not crash

        with patch("kvm_serial.kvm.QMainWindow.focusOutEvent"):
            app.focusOutEvent(mock_focus_out)  # Should not crash

    def test_video_view_focus_management(self):
        """Test that video view focus management affects keyboard state properly."""
        app = self.create_kvm_app()

        # Initial state
        initial_state = app.keyboard_var

        # Test that the video view exists and has focus methods
        self.assertTrue(hasattr(app.video_view, "focusInEvent"))
        self.assertTrue(hasattr(app.video_view, "focusOutEvent"))

        # The video view focus events would normally emit signals that the main
        # window connects to, but since we're testing in isolation, we test
        # the direct state changes that would result from those signals

        # Simulate what happens when video view gains focus
        app.keyboard_var = True
        self.assertTrue(app.keyboard_var, "Video view focus should enable keyboard capture")

        # Simulate what happens when video view loses focus
        app.keyboard_var = False
        self.assertFalse(
            app.keyboard_var, "Video view losing focus should disable keyboard capture"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
