#!/usr/bin/env python3
"""SMB2/3 write-read-delete smoke for the fnOS candidate (macOS host).

Uses the small ``smbprotocol`` client (no Samba install, no shell credential
history).  Reads the username/password from a chmod-600 credentials file, writes
one random payload, reads it back, compares SHA-256, deletes it and prints only
non-secret hashes/paths.

Usage:
  ~/.local/share/fnos-smb-venv/bin/python fnos/integration/hardware/smb_smoke.py \
    --host 192.168.3.x --share r3-smoke --credentials /tmp/r3-smb.cred
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path


def read_credentials(path: Path) -> tuple[str, str]:
    st = path.stat()
    if st.st_mode & 0o077:
        raise SystemExit("credentials file must be chmod 600")
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    if not values.get("username") or not values.get("password"):
        raise SystemExit("credentials file needs username= and password=")
    return values["username"], values["password"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--share", required=True)
    ap.add_argument("--credentials", required=True, type=Path)
    ap.add_argument("--size", type=int, default=8 * 1024 * 1024)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--no-encrypt", action="store_true")
    args = ap.parse_args()
    try:
        import smbclient
    except ImportError:
        raise SystemExit("run with ~/.local/share/fnos-smb-venv/bin/python")

    user, password = read_credentials(args.credentials)
    name = f"r3-smoke-{secrets.token_hex(4)}.bin"
    unc = rf"\\{args.host}\{args.share}\{name}"
    payload = os.urandom(args.size)
    src = hashlib.sha256(payload).hexdigest()
    result = {"schema": "fnos-smb-smoke/v1", "host": args.host, "share": args.share,
              "file": name, "bytes": args.size, "source_sha256": src}
    try:
        smbclient.register_session(args.host, username=user, password=password,
                                   port=445, encrypt=not args.no_encrypt)
        with smbclient.open_file(unc, mode="wb") as fh:
            fh.write(payload)
        with smbclient.open_file(unc, mode="rb") as fh:
            back = fh.read()
        result["readback_bytes"] = len(back)
        result["readback_sha256"] = hashlib.sha256(back).hexdigest()
        result["match"] = back == payload
        if not args.keep:
            smbclient.remove(unc)
            result["deleted"] = True
    except Exception as exc:  # noqa: BLE001 - report the SMB error, never the secret
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    result["ok"] = bool(result.get("match"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
