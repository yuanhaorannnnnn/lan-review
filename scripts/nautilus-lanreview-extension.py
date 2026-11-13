"""Nautilus context menu entry for the local LanReview importer."""
from pathlib import Path
import subprocess
from gi import require_version
require_version('Nautilus', '4.0')
from gi.repository import GObject, Nautilus

IMPORTER = Path('/media/yhr/2T/LanReview/scripts/nautilus-send-to-lanreview.py')
SUPPORTED = {'.md', '.markdown', '.html', '.htm', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.mp3', '.wav', '.m4a', '.ogg', '.flac', '.mp4', '.m4v', '.webm', '.mov'}


class LanReviewMenu(GObject.GObject, Nautilus.MenuProvider):
    def get_file_items(self, *args):
        files = args[-1]
        if len(files) != 1 or files[0].is_directory():
            return []
        path = files[0].get_location().get_path()
        if not path or Path(path).suffix.lower() not in SUPPORTED:
            return []
        item = Nautilus.MenuItem(name='LanReview::send', label='发送到 LanReview', tip='创建评审并打开')
        item.connect('activate', self._send, path)
        return [item]

    def _send(self, _item, path):
        subprocess.Popen([str(IMPORTER), path], start_new_session=True)
