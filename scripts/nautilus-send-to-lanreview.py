#!/usr/bin/env python3
"""Import one file from Nautilus into the local LanReview server."""
import json
import re
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

BASE = 'http://127.0.0.1:18765'
SUPPORTED = {'.md', '.markdown', '.html', '.htm', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.mp3', '.wav', '.m4a', '.ogg', '.flac', '.mp4', '.m4v', '.webm', '.mov'}


def fail(message):
    try:
        subprocess.run(['zenity', '--error', '--title=LanReview', '--text=' + message], check=False)
    except OSError:
        print(message, file=sys.stderr)
    return 1


def main(paths):
    if len(paths) != 1:
        return fail('请选中一个文件。')
    path = Path(paths[0]).absolute()
    if not path.is_file() or path.suffix.lower() not in SUPPORTED:
        return fail('请选择支持的 Markdown、HTML、图片或视频文件。')
    request = Request(BASE + '/api/reviews/import', data=json.dumps({'path': str(path)}).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=120) as response:
            result = json.load(response)
        review_id = result.get('id', '')
        if not isinstance(review_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', review_id):
            return fail('评审已创建，但返回的地址无法打开。')
        url = BASE + '/review/' + review_id
        subprocess.Popen(['xdg-open', url], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.get('warnings'):
            subprocess.Popen(['zenity', '--warning', '--title=LanReview', '--text=' + '\n'.join(result['warnings'])])
        return 0
    except HTTPError as error:
        try:
            message = json.load(error).get('error', '导入失败。')
        except (ValueError, OSError):
            message = '导入失败。'
        return fail(message)
    except (URLError, OSError, ValueError):
        return fail('无法连接 LanReview，请检查本机服务是否运行。')


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
