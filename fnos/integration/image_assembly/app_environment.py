"""Keep the home policy in the native application lifecycle environment.

fnOS ARM 1.2.0701 starts applications with a freshly constructed environment.
Its app_env values use the published binary's Obfuscate codec (XOR + base64).
Only our two non-secret settings are encoded here; other app settings are not
decoded. The binary and schema are checked before changing the compatibility
schema. Existing application-owned values are preserved and reported.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
from pathlib import Path
import re
import struct
import subprocess
import time

try:
    from .app_center_compat import reviewed_original
except ImportError:  # Installed standalone service entry.
    from app_center_compat import reviewed_original


def codec_mask(binary: bytes) -> bytes:
    binary = reviewed_original(binary)
    offset = struct.unpack_from("<Q", binary, 32)[0]
    entry_size, count = struct.unpack_from("<HH", binary, 54)
    # Read the public codec constant from the verified binary, not a user key.
    address = 0x16091B9
    for i in range(count):
        header = struct.unpack_from("<IIQQQQQQ", binary, offset + i * entry_size)
        if header[0] == 1 and header[3] <= address and address + 20 <= header[3] + header[5]:
            start = header[2] + address - header[3]
            return binary[start:start + 20]
    raise ValueError("native application environment codec not found")


def encode(value: str, mask: bytes) -> str:
    # The vendor uses base64.RawStdEncoding (padChar=-1). Padded values such
    # as short policy names are silently decoded to empty by its caller.
    return base64.b64encode(bytes(b ^ mask[i % len(mask)] for i, b in enumerate(value.encode()))).decode().rstrip("=")


def migration(library: str, mask: bytes) -> str:
    if not re.fullmatch(r"/usr/lib/fnos-x5/libhome_acl-[0-9a-f]{16}\.so", library):
        raise ValueError("unexpected home policy library path")
    values = {"LD_PRELOAD": encode(library, mask), "FNOS_HOME_DATA_ROOT": encode("auto", mask)}
    calls = "\n".join(f"PERFORM fnos_x5_home.apply_value(a.id, '{k}', '{v}');" for k, v in values.items())
    return """BEGIN;
SELECT pg_advisory_xact_lock(195935267);
CREATE SCHEMA IF NOT EXISTS fnos_x5_home;
CREATE TABLE IF NOT EXISTS fnos_x5_home.managed_env (
 app_id bigint NOT NULL, k text NOT NULL, applied_v text NOT NULL,
 previous_v text, PRIMARY KEY(app_id,k));
CREATE OR REPLACE FUNCTION fnos_x5_home.apply_value(aid bigint, key_name text, new_value text)
RETURNS void LANGUAGE plpgsql AS $body$
DECLARE current_value text; owned_value text; present boolean;
BEGIN
 SELECT v INTO current_value FROM public.app_env WHERE app_id=aid AND k=key_name;
 present := FOUND;
 SELECT applied_v INTO owned_value FROM fnos_x5_home.managed_env WHERE app_id=aid AND k=key_name;
 IF NOT present OR current_value IS NOT DISTINCT FROM owned_value THEN
  INSERT INTO fnos_x5_home.managed_env(app_id,k,applied_v,previous_v)
   VALUES(aid,key_name,new_value,current_value)
   ON CONFLICT(app_id,k) DO UPDATE SET applied_v=EXCLUDED.applied_v;
  INSERT INTO public.app_env(app_id,k,v) VALUES(aid,key_name,new_value)
   ON CONFLICT(app_id,k) DO UPDATE SET v=EXCLUDED.v;
 ELSE
  RAISE WARNING 'home storage configuration conflict: app_id=%, key=%', aid, key_name;
 END IF;
END $body$;
CREATE OR REPLACE FUNCTION fnos_x5_home.apply_app(a public.app)
RETURNS void LANGUAGE plpgsql AS $body$
BEGIN
 -- is_uninstall is the permission to offer Uninstall, not removal state.
 IF NOT a.is_docker THEN
""" + calls + """
 END IF;
END $body$;
CREATE OR REPLACE FUNCTION fnos_x5_home.app_changed()
RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
 PERFORM fnos_x5_home.apply_app(NEW);
 RETURN NEW;
END $body$;
DROP TRIGGER IF EXISTS fnos_x5_home_environment ON public.app;
CREATE TRIGGER fnos_x5_home_environment AFTER INSERT OR UPDATE ON public.app
 FOR EACH ROW EXECUTE FUNCTION fnos_x5_home.app_changed();
DO $body$ DECLARE a public.app; BEGIN
 FOR a IN SELECT * FROM public.app LOOP PERFORM fnos_x5_home.apply_app(a); END LOOP;
END $body$;
COMMIT;
"""


def psql(sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-d", "appcenter"],
        input=sql, text=True, capture_output=True, cwd="/tmp", timeout=20,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=int, default=0)
    args = parser.parse_args()
    mask = codec_mask(Path("/usr/trim/bin/trim_app_center").read_bytes())
    config = dict(line.split("=", 1) for line in Path("/etc/fnos-x5/home-storage.env").read_text().splitlines()
                  if line and not line.startswith("#"))
    library = config["LD_PRELOAD"]
    digest = hashlib.sha256(Path(library).read_bytes()).hexdigest()
    if not library.endswith(f"libhome_acl-{digest[:16]}.so"):
        raise ValueError("home policy library digest mismatch")
    deadline = time.monotonic() + args.wait
    while True:
        result = psql("SELECT to_regclass('public.app') IS NOT NULL AND to_regclass('public.app_env') IS NOT NULL;")
        if result.returncode == 0 and result.stdout.strip() == "t":
            break
        if time.monotonic() >= deadline:
            if args.wait:
                raise RuntimeError("application database schema unavailable")
            print("Initial application schema not ready; deferred to post-start hook")
            return
        time.sleep(1)
    # Schema drift fails transactionally, without partial environment writes.
    result = psql(migration(library, mask))
    if result.returncode:
        raise RuntimeError("application home-policy migration failed: " + result.stderr[:1000])
    if "configuration conflict" in result.stderr:
        raise RuntimeError(result.stderr[:1000])
    print("Native application home policy installed; credentials and unrelated settings preserved")


if __name__ == "__main__":
    main()
