from __future__ import annotations

from simple_pyspin import Camera


def get_image_with_timeout(camera: Camera, timeout_ms: int):
    """Retrieve next image with bounded wait when camera API supports timeout."""
    raw_cam = getattr(camera, "cam", None)
    if raw_cam is not None and hasattr(raw_cam, "GetNextImage"):
        return raw_cam.GetNextImage(timeout_ms)

    get_image = getattr(camera, "get_image", None)
    if get_image is None:
        raise RuntimeError("Camera object has no image retrieval method")

    timeout_variants = [
        ((), {"timeout": timeout_ms}),
        ((), {"timeout_ms": timeout_ms}),
        ((timeout_ms,), {}),
    ]
    last_type_error = None
    for args, kwargs in timeout_variants:
        try:
            return get_image(*args, **kwargs)
        except TypeError as exc:
            last_type_error = exc
            continue

    raise NotImplementedError("Camera API does not expose timeout-capable image retrieval") from last_type_error


def is_image_timeout_error(err: Exception) -> bool:
    text = str(err).lower()
    cls = err.__class__.__name__.lower()
    return "timeout" in text or "timed out" in text or "time out" in text or "timeout" in cls
