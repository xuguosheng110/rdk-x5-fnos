"""Version-pinned native AI configuration for the household image.

Only configuration defaults are inserted. Existing choices and recognition
results are never replaced. No identity, login credential or results DB is
shipped in the image. PostgreSQL uses the system's local peer authentication.
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess

AI_SHA = '47ea984c39ba920aeca003a6ad838a85aa6fb9ae1e8670305cf97698196d094f'
PHOTOS_SHA = 'd0916570db8cecab9c6e6e76dc5ddcc2cfa340834dd2a36c106d9cb2556fe976'
PHOTO_KEYS = ('ai_face_enabled', 'ai_smart_rec_enabled')
TRIGGER = 'fnos_x5_household_ai_defaults_v1'
TRIGGER_SQL = f'''CREATE TRIGGER {TRIGGER} AFTER INSERT ON user
BEGIN
  INSERT OR IGNORE INTO user_config(user_id,k,v,created_at,updated_at)
    VALUES(NEW.id,'ai_face_enabled','true',NEW.created_at,NEW.updated_at);
  INSERT OR IGNORE INTO user_config(user_id,k,v,created_at,updated_at)
    VALUES(NEW.id,'ai_smart_rec_enabled','true',NEW.created_at,NEW.updated_at);
END'''


def check_binary(path, expected):
    with Path(path).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise RuntimeError('unreviewed native AI binary: ' + str(path))


def ai_sql(index):
    if not re.fullmatch(r'[1-9][0-9]*', index):
        raise ValueError('invalid native data volume')
    # One transaction: a concurrent native writer wins via the unique key.
    # Re-check the selected volume after insertion, before commit.
    return '''BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='15s';
LOCK TABLE public.kv IN SHARE ROW EXCLUSIVE MODE;
DO $$ BEGIN
  IF (SELECT v FROM public.kv WHERE k='db_version') IS DISTINCT FROM 'v1.1.1' THEN
    RAISE EXCEPTION 'unreviewed native AI database version';
  END IF;
END $$;
WITH inserted AS (
  INSERT INTO public.kv(k,v) VALUES
    ('setting-data-vol','%s'),
    ('face-model-user-selected','GeneralV1.0.1'),
    ('imgTxtRec-model-user-selected','Medium')
  ON CONFLICT(k) DO NOTHING RETURNING k
) SELECT json_build_object('inserted',count(*)) FROM inserted;
DO $$ BEGIN
  IF (SELECT v FROM public.kv WHERE k='setting-data-vol') IS DISTINCT FROM '%s' THEN
    RAISE EXCEPTION 'existing native AI volume differs; choice preserved';
  END IF;
END $$;
COMMIT;
''' % (index, index)


def seed_ai(index, *, root=Path('/'), runner=subprocess.run):
    root = Path(root)
    check_binary(root / 'usr/trim/bin/ai_manager', AI_SHA)
    result = runner([str(root / 'usr/sbin/runuser'), '-u', 'postgres', '--',
                     str(root / 'usr/bin/psql'), '-X', '-qAt', '-v',
                     'ON_ERROR_STOP=1', '-d', 'ai_manager'],
                    input=ai_sql(index), capture_output=True, text=True, timeout=30,
                    cwd=str(root / 'tmp'))
    if result.returncode:
        raise RuntimeError('native AI configuration transaction failed; existing choices retained')
    data = json.loads(result.stdout)
    if type(data.get('inserted')) is not int or not 0 <= data['inserted'] <= 3:
        raise RuntimeError('unexpected native AI configuration response')
    return data


def seed_photos_database(path):
    """Apply to an existing native DB, never create an empty lookalike DB."""
    path = Path(path)
    connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)
    try:
        connection.execute('BEGIN IMMEDIATE')
        expected = {
            'user': [('id', 'INTEGER'), ('nas_uid', 'INTEGER'),
                     ('created_at', 'INTEGER'), ('updated_at', 'INTEGER')],
            'user_config': [('id', 'INTEGER'), ('user_id', 'INTEGER'), ('k', 'TEXT'),
                            ('v', 'TEXT'), ('created_at', 'INTEGER'), ('updated_at', 'INTEGER')],
        }
        for table, columns in expected.items():
            info = connection.execute('PRAGMA table_info(' + table + ')').fetchall()
            if [(r[1], r[2].upper()) for r in info] != columns or info[0][5] != 1:
                raise RuntimeError('unreviewed native Photos schema: ' + table)
        indices = connection.execute('PRAGMA index_list(user_config)').fetchall()
        unique = [r[1] for r in indices if r[2]]
        if not any([r[2] for r in connection.execute(
                'PRAGMA index_info("' + name.replace('"', '""') + '")')] == ['user_id', 'k']
                for name in unique):
            raise RuntimeError('native Photos configuration uniqueness missing')
        previous = connection.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?',
                                      ('trigger', TRIGGER)).fetchone()
        if previous and previous[0] != TRIGGER_SQL:
            raise RuntimeError('existing Photos default trigger differs')
        if not previous:
            connection.execute(TRIGGER_SQL)
        before = connection.total_changes
        for key in PHOTO_KEYS:
            connection.execute('''INSERT OR IGNORE INTO user_config(user_id,k,v,created_at,updated_at)
                SELECT id,?,'true',created_at,updated_at FROM user''', (key,))
        count = connection.total_changes - before
        connection.commit()
        return {'inserted': count, 'future_users': True, 'existing_choices': 'retained'}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def seed_photos(*, root=Path('/')):
    root = Path(root).resolve()
    app = root / 'var/apps/trim.photos'
    check_binary(app / 'target/trim-photos', PHOTOS_SHA)
    db = (app / 'var/db/photo.db').resolve(strict=True)
    # Native installations use /volN; the original ARM image also has a
    # legacy /usr/local/apps data tree. No arbitrary external symlink target.
    allowed = [root / 'usr/local/apps/@appdata/trim.photos/db/photo.db']
    allowed += [v / '@appdata/trim.photos/db/photo.db' for v in root.glob('vol[0-9]*')
                if re.fullmatch(r'vol[1-9][0-9]*', v.name) and not v.is_symlink()]
    if db not in allowed:
        raise RuntimeError('unexpected native Photos database location')
    return seed_photos_database(db)
