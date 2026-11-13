"""Remove expired, explicitly removed remote reviews from the host's data directory."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
import sys

ID = re.compile(r'^[A-Za-z0-9_-]{32}$')


def cleanup(data_dir, now=None):
    data_dir = Path(data_dir).resolve()
    database = data_dir / 'reviews.sqlite3'
    if not database.is_file():
        raise FileNotFoundError('review database not found')
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=30)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    removed = failed = 0
    with closing(sqlite3.connect(database, timeout=15)) as db:
        db.execute('PRAGMA foreign_keys = ON')
        ids = [row[0] for row in db.execute(
            'SELECT r.review_id FROM removed_reviews r JOIN review_origins o ON o.review_id = r.review_id '
            'WHERE o.remote = 1 AND r.removed_at IS NOT NULL AND r.removed_at <= ?', (cutoff,))]
        for review_id in ids:
            try:
                db.execute('BEGIN IMMEDIATE')
                eligible = db.execute('SELECT 1 FROM removed_reviews r JOIN review_origins o ON o.review_id = r.review_id WHERE r.review_id = ? AND o.remote = 1 AND r.removed_at IS NOT NULL AND r.removed_at <= ?', (review_id, cutoff)).fetchone()
                if not eligible:
                    db.rollback()
                    continue
                attachment_ids = [row[0] for row in db.execute('SELECT id FROM review_attachments WHERE review_id = ?', (review_id,))]
                if not ID.fullmatch(review_id) or any(not ID.fullmatch(i) for i in attachment_ids):
                    raise ValueError('invalid stored identifier')
                for name in [review_id, *(f'attachment-{i}' for i in attachment_ids)]:
                    (data_dir / 'files' / name).unlink(missing_ok=True)
                db.execute('DELETE FROM comment_thumbnails WHERE comment_id IN (SELECT id FROM comments WHERE review_id = ?)', (review_id,))
                db.execute('DELETE FROM comment_requests WHERE review_id = ?', (review_id,))
                db.execute('DELETE FROM comments WHERE review_id = ?', (review_id,))
                db.execute('DELETE FROM review_participants WHERE review_id = ?', (review_id,))
                db.execute('DELETE FROM review_attachments WHERE review_id = ?', (review_id,))
                db.execute('DELETE FROM removed_reviews WHERE review_id = ?', (review_id,))
                db.execute('DELETE FROM public_reviews WHERE review_id = ?', (review_id,))
                db.execute('DELETE FROM review_origins WHERE review_id = ?', (review_id,))
                db.execute('DELETE FROM reviews WHERE id = ?', (review_id,))
                db.commit()
                removed += 1
            except (OSError, sqlite3.Error, ValueError):
                db.rollback()
                failed += 1
    return removed, failed


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    count, errors = cleanup(args.data_dir)
    print(f'清理完成：{count} 项；失败：{errors} 项。')
    sys.exit(bool(errors))
