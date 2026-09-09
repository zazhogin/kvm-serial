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


def test_audio_device_prefers_device_linked_to_capture_card():
    video_device = MagicMock()
    video_device.hasMediaType_.return_value = False
    audio_device = MagicMock()
    audio_device.hasMediaType_.return_value = True
    video_device.linkedDevices.return_value = [audio_device]
    framework = MagicMock(AVMediaTypeAudio="audio")

    with patch.object(avf, "AVFoundation", framework):
        assert avf._find_audio_device(video_device) is audio_device


def test_audio_device_never_falls_back_to_unrelated_microphone():
    video_device = MagicMock()
    video_device.hasMediaType_.return_value = False
    video_device.linkedDevices.return_value = []
    video_device.localizedName.return_value = "Elgato 4K S"
    video_device.modelID.return_value = "elgato-video"
    microphone = MagicMock()
    microphone.localizedName.return_value = "MacBook Pro Microphone"
    microphone.modelID.return_value = "built-in-mic"
    framework = MagicMock(AVMediaTypeAudio="audio")

    with (
        patch.object(avf, "AVFoundation", framework),
        patch.object(avf, "_audio_devices", return_value=[microphone]),
    ):
        try:
            avf._find_audio_device(video_device)
        except RuntimeError as exc:
            assert "No HDMI audio input" in str(exc)
        else:
            raise AssertionError("An unrelated system microphone must not be selected")


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
    session.addOutput_.assert_not_called()
    session.setSessionPreset_.assert_called_once_with("input-priority")
    device.setActiveFormat_.assert_called_once_with(device_format)
    device.setActiveVideoMinFrameDuration_.assert_called_once_with("native-1/60")
    device.setActiveVideoMaxFrameDuration_.assert_called_once_with("native-1/60")
    device.unlockForConfiguration.assert_not_called()
    session.commitConfiguration.assert_called_once_with()
    preview_layer.setVideoGravity_.assert_called_once_with("aspect")
    attach.assert_called_once_with(preview_layer)
    thread.return_value.start.assert_called_once_with()
    framework.AVCaptureAudioPreviewOutput.alloc.assert_not_called()
    assert capture._audio_monitor is None
    assert capture.audio_monitoring is False

    capture.stop()
    device.unlockForConfiguration.assert_called_once_with()


def test_start_can_monitor_linked_hdmi_audio():
    video_device = MagicMock()
    video_device.lockForConfiguration_.return_value = (True, None)
    audio_device = MagicMock()
    audio_device.localizedName.return_value = "Elgato 4K S"
    device_format = MagicMock()
    frame_range = _frame_range(60, 60, duration="native-1/60")
    video_input = MagicMock()
    audio_input = MagicMock()
    video_session = MagicMock()
    video_session.canAddInput_.return_value = True
    video_session.canSetSessionPreset_.return_value = True
    audio_session = MagicMock()
    audio_session.canAddInput_.return_value = True
    audio_session.canAddOutput_.return_value = True
    preview_layer = MagicMock()
    audio_output = MagicMock()
    framework = MagicMock()
    framework.AVCaptureSession.alloc.return_value.init.side_effect = [
        video_session,
        audio_session,
    ]
    framework.AVCaptureDeviceInput.deviceInputWithDevice_error_.side_effect = [
        (video_input, None),
        (audio_input, None),
    ]
    framework.AVCaptureSessionPresetInputPriority = "input-priority"
    framework.AVCaptureVideoPreviewLayer.layerWithSession_.return_value = preview_layer
    framework.AVCaptureAudioPreviewOutput.alloc.return_value.init.return_value = audio_output
    framework.AVLayerVideoGravityResizeAspect = "aspect"

    with (
        patch.object(avf, "_IMPORT_ERROR", None),
        patch.object(avf, "AVFoundation", framework),
        patch.object(avf, "_find_device", return_value=video_device),
        patch.object(avf, "_find_audio_device", return_value=audio_device),
        patch.object(
            avf,
            "_select_format",
            return_value=(device_format, frame_range, 3840, 2160, 60.0),
        ),
        patch.object(avf.threading, "Thread"),
    ):
        capture = avf.AVFoundationPreviewCapture("elgato-id", MagicMock())
        with patch.object(capture, "_attach_preview_layer"):
            capture.start(3840, 2160)
            capture.set_audio_monitoring(True)

    assert capture.audio_monitoring is True
    assert capture._audio_monitor.device is audio_device
    assert capture._audio_monitor.device_input is audio_input
    assert capture._audio_monitor.preview_output is audio_output
    video_session.addInput_.assert_called_once_with(video_input)
    video_session.addOutput_.assert_not_called()
    audio_session.addInput_.assert_called_once_with(audio_input)
    audio_session.addOutput_.assert_called_once_with(audio_output)
    audio_output.setVolume_.assert_called_once_with(1.0)


def test_audio_monitoring_can_be_disabled_while_session_is_running():
    framework = MagicMock()
    with (
        patch.object(avf, "_IMPORT_ERROR", None),
        patch.object(avf, "AVFoundation", framework),
    ):
        capture = avf.AVFoundationPreviewCapture("elgato-id", MagicMock())

    capture.device = MagicMock()
    monitor = MagicMock()
    capture._audio_monitor = monitor
    capture.audio_monitoring = True

    assert capture.set_audio_monitoring(False) is False
    monitor.stop.assert_called_once_with()
    assert capture._audio_monitor is None


def test_active_mode_reads_back_real_resolution_and_rate():
    device = MagicMock()
    device.activeFormat.return_value.formatDescription.return_value = (1280, 720)
    device.activeVideoMinFrameDuration.return_value = "actual-duration"
    core_media = MagicMock()
    core_media.CMTimeGetSeconds.return_value = 1 / 30

    with (
        patch.object(avf, "_dimensions", _dimensions),
        patch.object(avf, "CoreMedia", core_media),
    ):
        assert avf._active_mode(device) == (1280, 720, 30.0)
