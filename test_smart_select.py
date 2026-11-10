"""Boundary tests for the optional local segmentation adapter."""
import io
from pathlib import Path
import tempfile
import unittest

import smart_select as smart

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, 'optional Pillow dependency is not installed')
class SmartSelectBoundaryTest(unittest.TestCase):
    @staticmethod
    def png(width=4, height=4):
        result = io.BytesIO()
        Image.new('RGB', (width, height)).save(result, format='PNG')
        return result.getvalue()

    def test_invalid_input_and_point(self):
        for image, x, y in [(b'', .5, .5), (b'<svg/>', .5, .5), (self.png(), float('nan'), 0), (self.png(), 1.1, 0), (self.png(), True, 0)]:
            with self.subTest(x=x, y=y), self.assertRaises(smart.SmartSelectError):
                smart.segment_image(image, x, y)

    def test_resource_limits_before_decode(self):
        with self.assertRaisesRegex(smart.SmartSelectError, '尺寸过大'):
            smart._decode(self.png(4000, 4000))
        with self.assertRaisesRegex(smart.SmartSelectError, '超过 12 MiB'):
            smart._decode(b'x' * (smart.MAX_IMAGE_BYTES + 1))

    def test_missing_and_untrusted_weights(self):
        with self.assertRaisesRegex(smart.SmartSelectError, '未安装'):
            smart.segment_image(self.png(), .5, .5, weights_path='/nonexistent/weights')
        with tempfile.NamedTemporaryFile() as file:
            file.write(b'not trusted weights')
            file.flush()
            stat = Path(file.name).stat()
            with self.assertRaisesRegex(smart.SmartSelectError, '校验失败'):
                smart._load_model(file.name, 'cpu', stat.st_mtime_ns, stat.st_size)

    def test_busy_returns_without_queueing(self):
        with tempfile.NamedTemporaryFile() as file:
            file.write(b'weights'); file.flush()
            # Existence is checked before the lock, but busy is checked before model loading.
            smart._MODEL_LOCK.acquire()
            try:
                with self.assertRaisesRegex(smart.SmartSelectError, '正忙'):
                    smart.segment_image(self.png(), .5, .5, weights_path=file.name)
            finally:
                smart._MODEL_LOCK.release()


if __name__ == '__main__':
    unittest.main()
