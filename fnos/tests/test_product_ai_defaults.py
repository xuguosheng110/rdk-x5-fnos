"""Exercise actual SQLite user creation and configuration preservation."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from fnos.integration.image_assembly import product_ai_defaults as defaults

SCHEMA = '''
CREATE TABLE user(id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
 nas_uid INTEGER,created_at INTEGER,updated_at INTEGER);
CREATE UNIQUE INDEX uniq_u_nas_uid ON user(nas_uid);
CREATE TABLE user_config(id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER,k TEXT,v TEXT,created_at INTEGER,updated_at INTEGER);
CREATE UNIQUE INDEX uniq_uc_user_id_k ON user_config(user_id,k);
CREATE TABLE recognition_result(id INTEGER PRIMARY KEY,result TEXT);
INSERT INTO recognition_result VALUES(1,'untouched');
'''


class NativeAIConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'photo.db'
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(SCHEMA)

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, query, args=()):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                return db.execute(query, args).fetchall()

    def test_new_user_defaults_use_internal_id_and_native_timestamps(self):
        defaults.seed_photos_database(self.path)
        self.rows('INSERT INTO user(id,nas_uid,created_at,updated_at) VALUES(7,1000,12345,12346)')
        self.assertEqual(self.rows('SELECT user_id,k,v,created_at,updated_at FROM user_config ORDER BY k'),
                         [(7, key, 'true', 12345, 12346) for key in defaults.PHOTO_KEYS])
        self.assertEqual(self.rows('SELECT * FROM recognition_result'), [(1, 'untouched')])

    def test_existing_false_and_later_user_choice_survive_retry(self):
        self.rows('INSERT INTO user(id,nas_uid) VALUES(5,1001)')
        self.rows("INSERT INTO user_config(user_id,k,v) VALUES(5,'ai_face_enabled','false')")
        self.assertEqual(defaults.seed_photos_database(self.path)['inserted'], 1)
        self.rows("UPDATE user_config SET v='false' WHERE k='ai_smart_rec_enabled'")
        self.assertEqual(defaults.seed_photos_database(self.path)['inserted'], 0)
        self.assertEqual(self.rows('SELECT v FROM user_config'), [('false',), ('false',)])

    def test_unknown_schema_does_not_write_any_configuration(self):
        self.rows('ALTER TABLE user_config ADD COLUMN upgraded INTEGER')
        with self.assertRaisesRegex(RuntimeError, 'schema'):
            defaults.seed_photos_database(self.path)
        self.assertEqual(self.rows('SELECT count(*) FROM user_config'), [(0,)])
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE type='trigger'"), [])

    def test_modified_owned_trigger_rejected_without_replacing(self):
        sql = 'CREATE TRIGGER ' + defaults.TRIGGER + ' AFTER INSERT ON user BEGIN SELECT 1; END'
        self.rows(sql)
        with self.assertRaisesRegex(RuntimeError, 'trigger differs'):
            defaults.seed_photos_database(self.path)
        self.assertEqual(self.rows("SELECT sql FROM sqlite_master WHERE type='trigger'"), [(sql,)])

    def test_missing_database_is_not_created(self):
        missing = self.path.with_name('absent.db')
        with self.assertRaises(sqlite3.OperationalError):
            defaults.seed_photos_database(missing)
        self.assertFalse(missing.exists())

    def test_volume_is_not_sql_or_path_input(self):
        for invalid in ('0', '../1', '1;COMMIT', '01', '-1', '1\n'):
            with self.assertRaises(ValueError):
                defaults.ai_sql(invalid)


if __name__ == '__main__':
    unittest.main()
