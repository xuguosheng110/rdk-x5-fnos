import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from fnos.integration.native_photos.native_face_entry import ensure_index_namespace


class FaceNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'photo.db'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('CREATE TABLE face(id); CREATE TABLE person(id);')
        self.marker = self.db.with_name('.fnos-face-namespace.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_index_can_be_bound_then_reopened(self):
        ensure_index_namespace('encoder-a', self.db)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO face VALUES(1)')
        ensure_index_namespace('encoder-a', self.db)
        self.assertEqual(json.loads(self.marker.read_text())['namespace'], 'encoder-a')

    def test_unknown_populated_index_rejected_without_deleting_data(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO face VALUES(1)')
        with self.assertRaisesRegex(RuntimeError, 'populated'):
            ensure_index_namespace('encoder-a', self.db)
        self.assertFalse(self.marker.exists())
        with closing(sqlite3.connect(self.db)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM face').fetchone()[0], 1)

    def test_namespace_change_rejected(self):
        ensure_index_namespace('encoder-a', self.db)
        with self.assertRaisesRegex(RuntimeError, 'model changed'):
            ensure_index_namespace('encoder-b', self.db)

    def test_marker_symlink_rejected(self):
        other = self.marker.with_name('other.json')
        other.write_text('{"namespace":"encoder-a"}')
        self.marker.symlink_to(other)
        with self.assertRaises(RuntimeError):
            ensure_index_namespace('encoder-a', self.db)

    @unittest.skipUnless(sys.platform in ('linux', 'darwin'), 'requires POSIX file-size limit')
    def test_failed_first_marker_write_can_retry_without_manual_cleanup(self):
        # Drive a real short write/EFBIG, not a mocked implementation detail.
        # The DB already exists; the child only attempts the namespace marker.
        script = '''
import resource, signal, sys
from fnos.integration.native_photos.native_face_entry import ensure_index_namespace
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (32, 32))
try:
    ensure_index_namespace('encoder-a', sys.argv[1])
except OSError:
    raise SystemExit(3)
raise SystemExit(0)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.db)],
                                cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(self.marker.exists(), 'partial marker would block every later startup')
        ensure_index_namespace('encoder-a', self.db)
        self.assertEqual(json.loads(self.marker.read_text())['namespace'], 'encoder-a')


if __name__ == '__main__':
    unittest.main()
