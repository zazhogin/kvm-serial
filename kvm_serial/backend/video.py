#!/usr/bin/env python
"""Camera enumeration for KVM Serial.

The cross-platform path uses QtMultimedia (QCamera / QCameraInfo), which wraps
DirectShow on Windows and V4L2 on Linux.  macOS capability enumeration uses
AVFoundation directly so high-frame-rate AVCaptureDeviceFormat entries hidden
by Qt 5 remain available.  The GUI retains QtMultimedia as a fallback when the
native bridge cannot load.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import logging
import sys

logger = logging.getLogger(__name__)

try:
    from PyQt5.QtCore import QEventLoop, QTimer
    from PyQt5.QtMultimedia import QCamera, QCameraInfo
except ImportError as e:  # pragma: no cover - environment-specific
    raise ImportError(
        "PyQt5 QtMultimedia is required for video capture. "
        "On Debian/Ubuntu, install with: "
        "apt install python3-pyqt5.qtmultimedia libqt5multimedia5-plugins"
    ) from e


# Maximum time to wait for QCamera.load() to populate supported settings.
# load() is documented as asynchronous; in practice AVFoundation/DirectShow
# return synchronously, but V4L2 may take a moment on first access.
PROBE_TIMEOUT_MS = 2000


class CaptureDeviceException(Exception):
    pass


@dataclass
class CameraProperties:
    """Capabilities of a camera device.

    Derived from QCameraInfo + QCamera.supportedViewfinderSettings(). The live
    QCameraInfo is retained so the GUI can pass it to QCamera() when opening.

    `index` is the position in the enumerated list; it has no relationship to
    any platform-native device index.
    """

    index: int
    name: str
    unique_id: str
    width: int
    height: int
    fps: int
    resolutions: List[Tuple[int, int]]
    default_resolution: Tuple[int, int]
    info: Optional[QCameraInfo] = None
    # macOS uses AVFoundation directly because Qt 5 hides high-frame-rate
    # AVCaptureDeviceFormat entries for some USB capture cards.  Other platforms
    # leave these fields at their backwards-compatible defaults.
    backend: str = "qt"
    fps_by_resolution: Optional[Dict[Tuple[int, int], float]] = None

    def __getitem__(self, key):
        return getattr(self, key)

    def __str__(self) -> str:
        return self.name  # f"{self.name} ({self.width}x{self.height}@{self.fps}fps)"


def _wait_for_loaded(cam: QCamera, timeout_ms: int = PROBE_TIMEOUT_MS) -> bool:
    """Spin a local event loop until the camera reaches LoadedStatus or times out."""
    if cam.status() == QCamera.LoadedStatus:
        return True

    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)

    def _on_status(status):
        if status == QCamera.LoadedStatus:
            loop.quit()

    cam.statusChanged.connect(_on_status)
    timer.start(timeout_ms)
    loop.exec_()
    cam.statusChanged.disconnect(_on_status)
    return cam.status() == QCamera.LoadedStatus


def _probe_camera(info: QCameraInfo, index: int) -> CameraProperties:
    """Load a camera in viewfinder-only mode to read its capabilities."""
    cam = QCamera(info)
    cam.load()
    if not _wait_for_loaded(cam):
        logger.warning(
            "Camera %d (%s) did not reach LoadedStatus within %dms; "
            "capabilities may be incomplete",
            index,
            info.description(),
            PROBE_TIMEOUT_MS,
        )

    settings_list = cam.supportedViewfinderSettings()
    seen: set = set()
    resolutions: List[Tuple[int, int]] = []
    max_fps = 0
    for s in settings_list:
        size = s.resolution()
        wh = (size.width(), size.height())
        if wh[0] > 0 and wh[1] > 0 and wh not in seen:
            seen.add(wh)
            resolutions.append(wh)
        fps = int(s.maximumFrameRate())
        if fps > max_fps:
            max_fps = fps

    # Sort by pixel count so the menu shows largest-first below "Use Default".
    resolutions.sort(key=lambda wh: (wh[0] * wh[1], wh[0]), reverse=True)

    current = cam.viewfinderSettings()
    current_size = current.resolution()
    if current_size.isValid() and current_size.width() > 0:
        default_res = (current_size.width(), current_size.height())
    elif resolutions:
        default_res = resolutions[0]
    else:
        default_res = (0, 0)

    cam.unload()

    # Only include default_res as a fallback when it is a valid (non-zero) size.
    # If both supportedViewfinderSettings() and viewfinderSettings() return nothing
    # useful, leave resolutions empty rather than propagating a "0x0" entry into
    # the GUI where it would appear as a selectable option and later be passed to
    # QCameraViewfinderSettings.setResolution(0, 0).
    if not resolutions and default_res[0] > 0:
        resolutions = [default_res]

    return CameraProperties(
        index=index,
        name=info.description() or info.deviceName() or f"Camera {index}",
        unique_id=info.deviceName() or str(index),
        width=default_res[0],
        height=default_res[1],
        fps=max_fps,
        resolutions=resolutions,
        default_resolution=default_res,
        info=info,
    )


def enumerate_cameras() -> List[CameraProperties]:
    """Return camera properties from native AVFoundation or QtMultimedia.

    Requires a running QCoreApplication (or QApplication). Safe to call from
    the main GUI thread; QCamera signals will be delivered via the local event
    loop spun by _wait_for_loaded.
    """
    infos = QCameraInfo.availableCameras()

    if sys.platform == "darwin":
        try:
            from kvm_serial.backend import macos_avfoundation

            native_cameras = macos_avfoundation.enumerate_cameras()
        except Exception as e:  # pragma: no cover - hardware/framework dependent
            native_cameras = []
            logger.warning("Native AVFoundation camera enumeration failed: %s", e)

        if native_cameras:
            qt_infos_by_id = {str(info.deviceName()): info for info in infos}
            qt_infos_by_name = {str(info.description()): info for info in infos}
            cameras: List[CameraProperties] = []
            for i, native in enumerate(native_cameras):
                fps_by_resolution: Dict[Tuple[int, int], float] = {}
                for mode in native.modes:
                    resolution = (mode.width, mode.height)
                    fps_by_resolution[resolution] = max(
                        fps_by_resolution.get(resolution, 0.0), mode.max_fps
                    )
                resolutions = sorted(
                    fps_by_resolution,
                    key=lambda wh: (wh[0] * wh[1], fps_by_resolution[wh], wh[0]),
                    reverse=True,
                )
                default_res = resolutions[0] if resolutions else (0, 0)
                cameras.append(
                    CameraProperties(
                        index=i,
                        name=native.name,
                        unique_id=native.unique_id,
                        width=default_res[0],
                        height=default_res[1],
                        fps=int(round(max(fps_by_resolution.values(), default=0.0))),
                        resolutions=resolutions,
                        default_resolution=default_res,
                        info=qt_infos_by_id.get(native.unique_id)
                        or qt_infos_by_name.get(native.name),
                        backend="avfoundation",
                        fps_by_resolution=fps_by_resolution,
                    )
                )
            logger.info("Found %d cameras via native AVFoundation.", len(cameras))
            for camera in cameras:
                logger.info(
                    "AVFoundation camera %s modes: %s",
                    camera.name,
                    ", ".join(
                        f"{w}x{h}@{camera.fps_by_resolution[(w, h)]:.3f}"
                        for w, h in camera.resolutions
                    ),
                )
            return cameras

    cameras: List[CameraProperties] = []
    for i, info in enumerate(infos):
        try:
            cameras.append(_probe_camera(info, i))
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to probe camera %d (%s): %s", i, info.description(), e)
    logger.info("Found %d cameras via QtMultimedia.", len(cameras))
    logger.debug(cameras)
    return cameras


class CaptureDevice:
    """Backwards-compatible namespace exposing the enumeration entrypoint.

    The previous class wrapped cv2.VideoCapture and ran a frame-capture loop in
    a worker thread. Capture now streams directly into a platform video sink,
    so there is no per-instance state to hold here.
    """

    @staticmethod
    def getCameras() -> List[CameraProperties]:
        return enumerate_cameras()
