"""Unit tests for the native macOS high-frame-rate capture backend."""

from unittest.mock import MagicMock, patch

from kvm_serial.backend import macos_avfoundation as avf


def _frame_range(min_fps, max_fps, duration=None):
    frame_range = MagicMock()
    frame_range.minFrameRate.return_value = min_fps
    frame_range.maxFrameRate.return_value = max_fps
    frame_range.minFrameDuration.return_value = duration
    return frame_range


def _device_format(width, height, *ranges):
    device_format = MagicMock()
    device_format.formatDescription.return_value = (width, height)
    device_format.videoSupportedFrameRateRanges.return_value = list(ranges)
    return device_format


def _dimensions(description):
    return description


def test_select_format_picks_4k60_instead_of_4k30():
    mode_4k30 = _device_format(3840, 2160, _frame_range(30, 30))
    range_4k60 = _frame_range(60, 60, duration="1/60")
    mode_4k60 = _device_format(3840, 2160, range_4k60)
    device = MagicMock()
    device.formats.return_value = [mode_4k30, mode_4k60]

    with patch.object(avf, "_dimensions", _dimensions):
        selected, frame_range, width, height, fps = avf._select_format(device, 3840, 2160)

    assert selected is mode_4k60
    assert frame_range is range_4k60
    assert (width, height, fps) == (3840, 2160, 60.0)


def test_default_prefers_resolution_before_frame_rate():
    mode_1080p120 = _device_format(1920, 1080, _frame_range(120, 120))
    mode_4k60 = _device_format(3840, 2160, _frame_range(60, 60))
    device = MagicMock()
    device.formats.return_value = [mode_1080p120, mode_4k60]

    with patch.object(avf, "_dimensions", _dimensions):
        selected, _, width, height, fps = avf._select_format(device, 0, 0)

    assert selected is mode_4k60
    assert (width, height, fps) == (3840, 2160, 60.0)


def test_enumeration_deduplicates_pixel_formats_and_keeps_rates():
    range_60 = _frame_range(60, 60)
    duplicate_a = _device_format(3840, 2160, range_60)
    duplicate_b = _device_format(3840, 2160, range_60)
    mode_1080 = _device_format(1920, 1080, _frame_range(30, 120))
    device = MagicMock()
    device.localizedName.return_value = "Elgato"
    device.uniqueID.return_value = "elgato-id"
    device.formats.return_value = [duplicate_a, duplicate_b, mode_1080]

    with (
        patch.object(avf, "_video_devices", return_value=[device]),
        patch.object(avf, "_dimensions", _dimensions),
    ):
        cameras = avf.enumerate_cameras()

    assert len(cameras) == 1
    assert cameras[0].name == "Elgato"
    assert cameras[0].unique_id == "elgato-id"
    assert cameras[0].modes == (
        avf.AVFoundationMode(3840, 2160, 60.0, 60.0),
        avf.AVFoundationMode(1920, 1080, 30.0, 120.0),
    )


def test_result_and_error_normalises_pyobjc_out_parameter_results():
    assert avf._result_and_error((True, None)) == (True, None)
    assert avf._result_and_error("object") == ("object", None)


def test_start_locks_exact_format_and_frame_duration():
    device = MagicMock()
    device.lockForConfiguration_.return_value = (True, None)
    device_format = MagicMock()
    frame_range = _frame_range(60, 60, duration="native-1/60")
    device_input = MagicMock()
    session = MagicMock()
    session.canAddInput_.return_value = True
    session.canSetSessionPreset_.return_value = True
    preview_layer = MagicMock()
    framework = MagicMock()
    framework.AVCaptureSession.alloc.return_value.init.return_value = session
    framework.AVCaptureDeviceInput.deviceInputWithDevice_error_.return_value = (
        device_input,
        None,
    )
    framework.AVCaptureSessionPresetInputPriority = "input-priority"
    framework.AVCaptureVideoPreviewLayer.layerWithSession_.return_value = preview_layer
    framework.AVLayerVideoGravityResizeAspect = "aspect"

    with (
        patch.object(avf, "_IMPORT_ERROR", None),
        patch.object(avf, "AVFoundation", framework),
        patch.object(avf, "_find_device", return_value=device),
        patch.object(
            avf,
            "_select_format",
            return_value=(device_format, frame_range, 3840, 2160, 60.0),
        ),
        patch.object(avf.threading, "Thread") as thread,
    ):
        capture = avf.AVFoundationPreviewCapture("elgato-id", MagicMock())
        with patch.object(capture, "_attach_preview_layer") as attach:
            result = capture.start(3840, 2160)

    assert result == (3840, 2160, 60.0)
    session.beginConfiguration.assert_called_once_with()
    session.addInput_.assert_called_once_with(device_input)
    session.setSessionPreset_.assert_called_once_with("input-priority")
    device.setActiveFormat_.assert_called_once_with(device_format)
    device.setActiveVideoMinFrameDuration_.assert_called_once_with("native-1/60")
    device.setActiveVideoMaxFrameDuration_.assert_called_once_with("native-1/60")
    device.unlockForConfiguration.assert_called_once_with()
    session.commitConfiguration.assert_called_once_with()
    preview_layer.setVideoGravity_.assert_called_once_with("aspect")
    attach.assert_called_once_with(preview_layer)
    thread.return_value.start.assert_called_once_with()
