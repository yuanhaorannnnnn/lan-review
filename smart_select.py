"""Optional, local-only point-prompted image segmentation with MobileSAM.

The caller must obtain image pixels locally and require an explicit user action.
No network access, automatic model download, semantic identification, or tracking
is performed. A returned outline is a suggestion, never a confirmed annotation.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
from io import BytesIO
import math
import os
from pathlib import Path
import sys
import threading

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_IMAGE_DIMENSION = 8192
MAX_OUTLINE_POINTS = 256
_ROOT = Path(__file__).resolve().parent
_VENDOR = _ROOT / 'vendor' / 'mobilesam'
_DEFAULT_WEIGHTS = _ROOT / 'models' / 'mobile_sam.pt'
_WEIGHTS_SHA256 = '6dbb90523a35330fedd7f1d3dfc66f995213d81b29a5ca8108dbcdd4e37d6c2f'
_MODEL_LOCK = threading.Lock()


class SmartSelectError(Exception):
    """Expected local model, input, or inference failure; use manual selection."""


def _coordinates(x, y):
    for value in (x, y):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise SmartSelectError('选点坐标必须在画面范围内。')
    return float(x), float(y)


def _decode(image_bytes):
    if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise SmartSelectError('图片为空或超过 12 MiB。')
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise SmartSelectError('智能选物依赖未安装，请改用手动选区。') from exc
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format not in {'PNG', 'JPEG', 'WEBP'}:
                raise SmartSelectError('智能选物仅支持 PNG、JPEG 或 WebP 画面。')
            width, height = image.size
            if width < 1 or height < 1 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                raise SmartSelectError('画面尺寸过大，请改用手动选区。')
            # Match browser-displayed orientation for photographs with EXIF metadata.
            return ImageOps.exif_transpose(image).convert('RGB')
    except SmartSelectError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise SmartSelectError('无法读取画面，请改用手动选区。') from exc


@lru_cache(maxsize=2)
def _load_model(weights_path, device, _mtime_ns, _size):
    try:
        with open(weights_path, 'rb') as weights:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: weights.read(1024 * 1024), b''):
                hasher.update(chunk)
        if hasher.hexdigest() != _WEIGHTS_SHA256:
            raise SmartSelectError('本机智能选物模型校验失败，请改用手动选区。')
    except OSError as exc:
        raise SmartSelectError('本机智能选物模型无法读取，请改用手动选区。') from exc
    try:
        import torch
        # This bundled, Apache-2.0 implementation does not download assets.
        if str(_VENDOR) not in sys.path:
            sys.path.insert(0, str(_VENDOR))
        from mobile_sam import sam_model_registry
    except (ImportError, OSError, RuntimeError) as exc:
        raise SmartSelectError('智能选物依赖不可用，请改用手动选区。') from exc
    actual_device = ('cuda' if torch.cuda.is_available() else 'cpu') if device == 'auto' else device
    if actual_device == 'cuda' and not torch.cuda.is_available():
        raise SmartSelectError('本机 GPU 不可用，可改用 CPU 或手动选区。')
    try:
        model = sam_model_registry['vit_t'](checkpoint=weights_path)
        model.to(device=actual_device).eval()
    except Exception as exc:
        raise SmartSelectError('智能选物模型无法加载，请检查本机模型文件或改用手动选区。') from exc
    return model, actual_device


def _outline(mask, width, height):
    import cv2
    import numpy as np
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    # A single outline can only encode one continuous area; choose the largest.
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 2:
        return None
    perimeter = cv2.arcLength(contour, True)
    epsilon = max(0.5, perimeter * .001)
    simplified = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
    while len(simplified) > MAX_OUTLINE_POINTS and epsilon < perimeter:
        epsilon *= 1.5
        simplified = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
    if len(simplified) < 3:
        return None
    return [[round(float(px) / width, 6), round(float(py) / height, 6)] for px, py in simplified]


def segment_image(image_bytes, x, y, *, weights_path=None, device='auto'):
    """Return local point-selection suggestions for an explicitly supplied frame.

    `x`, `y` are relative to the displayed image (0..1). Returns image dimensions,
    inference device, and up to three unordered candidates containing `points` and
    `score`. Outlines approximate only the largest connected component; holes and
    other components are omitted. Scores are model rankings, not probabilities.
    Resource use and GPU access are serialized to avoid overlapping encoders.
    """
    x, y = _coordinates(x, y)
    if device not in {'auto', 'cpu', 'cuda'}:
        raise SmartSelectError('智能选物设备配置无效。')
    if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise SmartSelectError('图片为空或超过 12 MiB。')
    path = Path(weights_path or os.environ.get('LANREVIEW_SMART_WEIGHTS') or _DEFAULT_WEIGHTS).expanduser()
    try:
        stat = path.stat()
        if not path.is_file() or not 0 < stat.st_size <= 100 * 1024 * 1024:
            raise OSError('invalid weights')
    except OSError as exc:
        raise SmartSelectError('本机智能选物模型未安装，请改用手动选区。') from exc
    try:
        import numpy as np
        import torch
        import cv2  # noqa: F401
    except (ImportError, OSError) as exc:
        raise SmartSelectError('智能选物依赖未安装，请改用手动选区。') from exc
    if not _MODEL_LOCK.acquire(blocking=False):
        raise SmartSelectError('智能选物正忙，请稍后重试或使用手动选区。')
    try:
        image = _decode(image_bytes)
        width, height = image.size
        with torch.inference_mode():
            model, actual_device = _load_model(str(path.resolve()), device, stat.st_mtime_ns, stat.st_size)
            from mobile_sam import SamPredictor
            predictor = SamPredictor(model)
            predictor.set_image(np.asarray(image))
            masks, scores, _ = predictor.predict(
                point_coords=np.array([[min(width - 1, int(x * width)), min(height - 1, int(y * height))]]),
                point_labels=np.array([1]), multimask_output=True,
            )
            candidates = []
            for mask, score in zip(masks, scores):
                points = _outline(mask, width, height)
                if points:
                    candidates.append({'points': points, 'score': round(float(score), 4)})
            return {'width': width, 'height': height, 'device': actual_device, 'candidates': candidates}
    except SmartSelectError:
        raise
    except Exception as exc:
        raise SmartSelectError('智能选物暂不可用，请改用手动选区。') from exc
    finally:
        _MODEL_LOCK.release()
