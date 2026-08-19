"""Low-latency macOS camera capture using AVFoundation directly.

Qt 5's AVFoundation camera plugin does not expose every high-frame-rate
``AVCaptureDeviceFormat`` offered by USB capture cards.  This module bypasses
that plugin on macOS, explicitly selects a native format/rate, and displays it
with ``AVCaptureVideoPreviewLayer``.  The preview layer is GPU/Core Animation
backed and does not copy frames through Python or maintain a Python frame FIFO.

The imports are deliberately optional.  Non-macOS installations, and macOS
environments created before the PyObjC AVFoundation dependency was added, can
continue to use the QtMultimedia backend.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - availability is platform/environment specific
    import objc
    import AVFoundation
    import CoreMedia
except ImportError as exc:  # pragma: no cover - exercised through availability checks
    objc = None
    AVFoundation = None
    CoreMedia = None
    _IMPORT_ERROR: Optional[ImportError] = exc
else:  # pragma: no cover - only run on macOS with PyObjC frameworks installed
    _IMPORT_ERROR = None


@dataclass(frozen=True)
class AVFoundationMode:
    """A resolution/rate combination exposed by an AVCaptureDeviceFormat."""

    width: int
    height: int
    min_fps: float
    max_fps: float


@dataclass(frozen=True)
class AVFoundationCamera:
    """The native camera information needed by the platform-neutral GUI model."""

    name: str
    unique_id: str
    modes: tuple[AVFoundationMode, ...]


def is_available() -> bool:
    """Return whether the PyObjC AVFoundation bridge is importable."""

    return _IMPORT_ERROR is None


def import_error() -> Optional[ImportError]:
    """Return the import error used to explain why the native backend is unavailable."""

    return _IMPORT_ERROR


def _dimensions(format_description: Any) -> tuple[int, int]:
    dimensions = CoreMedia.CMVideoFormatDescriptionGetDimensions(format_description)
    # PyObjC has represented CMVideoDimensions as both a struct-like object and
    # a two-item tuple across releases.  Supporting both costs almost nothing.
    if hasattr(dimensions, "width"):
        return int(dimensions.width), int(dimensions.height)
    return int(dimensions[0]), int(dimensions[1])


def _format_modes(device_format: Any) -> Iterable[AVFoundationMode]:
    width, height = _dimensions(device_format.formatDescription())
    if width <= 0 or height <= 0:
        return
    for frame_range in device_format.videoSupportedFrameRateRanges():
        yield AVFoundationMode(
            width=width,
            height=height,
            min_fps=float(frame_range.minFrameRate()),
            max_fps=float(frame_range.maxFrameRate()),
        )


def _video_devices() -> list[Any]:
    if not is_available():
        return []
    # This API is deprecated in favour of discovery sessions but remains the
    # only stable way to include both old ExternalUnknown devices and the newer
    # External device type on all supported macOS/PyObjC combinations.
    return list(AVFoundation.AVCaptureDevice.devicesWithMediaType_(AVFoundation.AVMediaTypeVideo))


def enumerate_cameras() -> list[AVFoundationCamera]:
    """Enumerate native AVFoundation devices and all of their video modes."""

    if not is_available():
        return []

    cameras: list[AVFoundationCamera] = []
    for device in _video_devices():
        # Deduplicate identical ranges reported for different pixel formats;
        # format selection still inspects the original formats when opening.
        modes = {
            mode for device_format in device.formats() for mode in _format_modes(device_format)
        }
        ordered_modes = tuple(
            sorted(
                modes,
                key=lambda mode: (mode.width * mode.height, mode.max_fps, mode.width),
                reverse=True,
            )
        )
        cameras.append(
            AVFoundationCamera(
                name=str(device.localizedName()),
                unique_id=str(device.uniqueID()),
                modes=ordered_modes,
            )
        )
    return cameras


def _find_device(unique_id: str) -> Any:
    if not is_available():
        raise RuntimeError(f"AVFoundation is unavailable: {_IMPORT_ERROR}")
    device = AVFoundation.AVCaptureDevice.deviceWithUniqueID_(unique_id)
    if device is None:
        raise RuntimeError(f"AVFoundation camera {unique_id!r} is no longer connected")
    return device


def _select_format(device: Any, width: int, height: int) -> tuple[Any, Any, int, int, float]:
    """Pick the requested resolution's fastest native device format.

    A zero requested size means "largest resolution".  If a non-zero requested
    size disappeared after enumeration (for example after an HDMI mode change),
    the largest available resolution is used instead of failing camera startup.
    """

    candidates: list[tuple[int, float, Any, Any, int, int]] = []
    fallback: list[tuple[int, float, Any, Any, int, int]] = []
    for device_format in device.formats():
        format_width, format_height = _dimensions(device_format.formatDescription())
        for frame_range in device_format.videoSupportedFrameRateRanges():
            max_fps = float(frame_range.maxFrameRate())
            entry = (
                format_width * format_height,
                max_fps,
                device_format,
                frame_range,
                format_width,
                format_height,
            )
            fallback.append(entry)
            if width > 0 and height > 0 and (format_width, format_height) == (width, height):
                candidates.append(entry)

    pool = candidates if candidates else fallback
    if not pool:
        raise RuntimeError("The AVFoundation camera exposes no video formats")

    # For an explicit resolution, pixel count is equal and max_fps wins.  For
    # the default path, resolution wins first and rate second (4K60 over 4K30).
    _, max_fps, device_format, frame_range, selected_w, selected_h = max(
        pool, key=lambda entry: (entry[0], entry[1])
    )
    return device_format, frame_range, selected_w, selected_h, max_fps


def _result_and_error(result: Any) -> tuple[Any, Any]:
    """Normalise PyObjC NSError-out method results across bridge versions."""

    if isinstance(result, tuple) and len(result) == 2:
        return result
    return result, None


class AVFoundationPreviewCapture:
    """Own an AVCaptureSession and a zero-copy preview layer hosted by a Qt widget."""

    def __init__(
        self,
        unique_id: str,
        host_widget: Any,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not is_available():
            raise RuntimeError(f"AVFoundation is unavailable: {_IMPORT_ERROR}")
        self.unique_id = unique_id
        self.host_widget = host_widget
        self.on_error = on_error
        self.session: Any = None
        self.preview_layer: Any = None
        self._host_layer: Any = None
        self._runner: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self.width = 0
        self.height = 0
        self.fps = 0.0

    def start(self, width: int = 0, height: int = 0) -> tuple[int, int, float]:
        """Configure the requested size at its maximum supported rate and start preview."""

        device = _find_device(self.unique_id)
        device_format, frame_range, selected_w, selected_h, max_fps = _select_format(
            device, width, height
        )

        session = AVFoundation.AVCaptureSession.alloc().init()
        session.beginConfiguration()
        try:
            device_input_result = AVFoundation.AVCaptureDeviceInput.deviceInputWithDevice_error_(
                device, None
            )
            device_input, input_error = _result_and_error(device_input_result)
            if device_input is None:
                raise RuntimeError(f"Could not create camera input: {input_error}")
            if not session.canAddInput_(device_input):
                raise RuntimeError("AVFoundation capture session rejected the camera input")
            session.addInput_(device_input)

            # InputPriority stops the session from replacing the explicit high
            # frame-rate format with a conventional 30 fps preset on macOS.
            preset = AVFoundation.AVCaptureSessionPresetInputPriority
            if session.canSetSessionPreset_(preset):
                session.setSessionPreset_(preset)

            locked, lock_error = _result_and_error(device.lockForConfiguration_(None))
            if not locked:
                raise RuntimeError(f"Could not lock camera configuration: {lock_error}")
            try:
                device.setActiveFormat_(device_format)
                # minFrameDuration corresponds to maxFrameRate.  Applying the
                # exact native CMTime also handles 59.94 without rounding to 60.
                frame_duration = frame_range.minFrameDuration()
                device.setActiveVideoMinFrameDuration_(frame_duration)
                device.setActiveVideoMaxFrameDuration_(frame_duration)
            finally:
                device.unlockForConfiguration()
        finally:
            session.commitConfiguration()

        preview_layer = AVFoundation.AVCaptureVideoPreviewLayer.layerWithSession_(session)
        preview_layer.setVideoGravity_(AVFoundation.AVLayerVideoGravityResizeAspect)
        self._attach_preview_layer(preview_layer)

        self.session = session
        self.preview_layer = preview_layer
        self.width = selected_w
        self.height = selected_h
        self.fps = max_fps
        self._stopping.clear()
        self._runner = threading.Thread(
            target=self._start_running,
            name="avfoundation-capture",
            daemon=True,
        )
        self._runner.start()
        logger.info(
            "AVFoundation preview started: %dx%d @ %.3f fps (GPU preview layer)",
            selected_w,
            selected_h,
            max_fps,
        )
        return selected_w, selected_h, max_fps

    def _attach_preview_layer(self, preview_layer: Any) -> None:
        # Force a native Cocoa view for the QGraphicsView viewport and bridge its
        # NSView pointer without creating another child window that could steal
        # Qt mouse/keyboard events.
        # Calling winId() forces Qt to create a native NSView for the viewport.
        ns_view = objc.objc_object(c_void_p=int(self.host_widget.winId()))
        ns_view.setWantsLayer_(True)
        host_layer = ns_view.layer()
        host_layer.addSublayer_(preview_layer)
        self._host_layer = host_layer
        preview_layer.setFrame_(host_layer.bounds())

    def _start_running(self) -> None:
        session = self.session
        try:
            with objc.autorelease_pool():
                if not self._stopping.is_set():
                    session.startRunning()
                if self._stopping.is_set() and session.isRunning():
                    session.stopRunning()
        except Exception as exc:  # pragma: no cover - hardware/driver dependent
            logger.exception("AVFoundation failed while starting capture")
            if self.on_error is not None:
                self.on_error(str(exc))

    def set_display_rect(
        self, x: float, y: float, width: float, height: float, viewport_height: float
    ) -> None:
        """Keep the native layer aligned with the transformed QGraphicsVideoItem."""

        if self.preview_layer is None:
            return
        # Core Animation's default origin is bottom-left; Qt viewport coordinates
        # start at top-left.
        layer_y = max(0.0, float(viewport_height) - float(y) - float(height))
        self.preview_layer.setFrame_(
            ((float(x), layer_y), (max(0.0, float(width)), max(0.0, float(height))))
        )

    def stop(self) -> None:
        """Stop capture and detach the preview layer.  Safe to call repeatedly."""

        self._stopping.set()
        if self.session is not None and self.session.isRunning():
            self.session.stopRunning()
        if self.preview_layer is not None:
            self.preview_layer.removeFromSuperlayer()
        self.preview_layer = None
        self._host_layer = None
        self.session = None
