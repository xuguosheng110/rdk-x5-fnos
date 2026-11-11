#!/usr/bin/env python3
"""Build the one-shot post-flash boot acceptance from a completed local deploy.

The historical flow had Codex write this record after independently re-reading
the backup and read-back evidence.  Codex is no longer in the loop (2026-11-06
handoff); this generator re-checks the same values and labels the record as
DeepSeek self-verification.  It never touches hardware and refuses any
incomplete/mismatched evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CANDIDATE_SHA256 = "653ffe7a5e20b019f0a5192799c5ddbda3752ffe5d8f6d67ba87e1afc498f599"
CANDIDATE_SIZE = 13157531648
CARD_BYTES = 31914983424
SCHEMA = "fnos-postflash-boot-acceptance/v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"evidence is not a regular file: {path}")
    return json.loads(path.read_text())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence-dir", type=Path, required=True)
    ap.add_argument("--reviewed-target", type=Path,
                    default=ROOT / "fnos/coordination/codex-ums-reviewed-target-20261106.json")
    ap.add_argument("--device", default="/dev/disk12")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()

    backup_path = args.evidence_dir / "backup-evidence.json"
    readback_path = args.evidence_dir / "readback-evidence.json"
    backup = load_json(backup_path)
    readback = load_json(readback_path)
    reviewed = json.loads(args.reviewed_target.read_text())

    problems = []
    if backup.get("ok") is not True: problems.append("backup.ok is not true")
    if backup.get("skipped") is True:
        # Explicit user decision: the card was empty and is overwritten
        # without a whole-card backup.  Never treat this as a verified backup.
        if backup.get("authorized_by") != "user":
            problems.append("skipped backup lacks authorized_by=user")
        if not backup.get("reason"):
            problems.append("skipped backup lacks a reason")
    else:
        if backup.get("bytes") != CARD_BYTES: problems.append("backup.bytes is not the full card")
        if backup.get("rehash_verified") is not True: problems.append("backup.rehash_verified is not true")
        if not isinstance(backup.get("sha256"), str) or len(backup["sha256"]) != 64:
            problems.append("backup.sha256 is missing")
    if readback.get("ok") is not True: problems.append("readback.ok is not true")
    if readback.get("bytes") != CANDIDATE_SIZE: problems.append("readback.bytes is not the image size")
    if readback.get("readback_sha256") != CANDIDATE_SHA256:
        problems.append("readback.readback_sha256 is not the candidate SHA")
    if readback.get("image_sha256") != CANDIDATE_SHA256:
        problems.append("readback.image_sha256 is not the candidate SHA")
    if problems:
        raise SystemExit("; ".join(problems))

    target = {key: reviewed[key] for key in (
        "model", "size_bytes", "usb_gadget_serial", "usb_vid", "usb_pid",
        "usb_location_id", "usb_path", "whole", "internal", "bus")}
    target["device"] = args.device

    out = args.output or (args.evidence_dir / "postflash-boot-acceptance.json")
    if out.exists():
        raise SystemExit(f"acceptance already exists: {out}")
    value = {
        "schema": SCHEMA,
        "accepted": True,
        "reviewer": "DeepSeek Harness (self-verified)",
        "reviewer_independence": "self",
        "scope": "one-shot first hardware boot of the exact r3 image; NOT independent review and NOT NAS/product acceptance",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "candidate": {
            "sha256": CANDIDATE_SHA256,
            "size": CANDIDATE_SIZE,
            "kernel": "6.18.0",
            "root_uuid": "c2d3e4f5-a6b7-4c8d-9e0f-1a2b3c4d5e6f",
            "fnos_version": "1.2.0701",
        },
        "target": target,
        "evidence": {
            "backup": {"path": str(backup_path), "sha256": sha256_file(backup_path)},
            "readback": {"path": str(readback_path), "sha256": sha256_file(readback_path)},
        },
    }
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    print(json.dumps({"ok": True, "acceptance": str(out),
                      "sha256": hashlib.sha256(raw).hexdigest()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
