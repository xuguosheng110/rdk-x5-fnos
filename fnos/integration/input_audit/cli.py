"""CLI for the read-only fnOS input audit."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from .audit import Auditor, SCHEMA, TOOL_VERSION
from .report import render


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m fnos.integration.input_audit",
        description="Read-only static audit of an fnOS ARM image. No mount, no execution, no sudo.",
    )
    parser.add_argument("--version", action="version", version=f"fnos-input-audit {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command")
    audit = sub.add_parser("audit", help="audit an image or a directory fixture")
    source = audit.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path, help="official .img/.img.gz/.iso input")
    source.add_argument("--rootfs-dir", type=Path, help="directory fixture (tests only; not a real image)")
    audit.add_argument("--work-dir", type=Path, default=Path(".fnos-input-audit-work"), help="private work directory for decompression/partition copies")
    audit.add_argument("--out-json", type=Path, required=True)
    audit.add_argument("--out-md", type=Path)
    audit.add_argument("--official-md5", help="MD5 published by the vendor download page")
    audit.add_argument("--source-url", help="original first-party URL (or official mirror listed by the vendor)")
    audit.add_argument("--kernel-config", type=Path, help="same-build X5 6.18 .config")
    audit.add_argument("--kernel-image", type=Path, help="same-build X5 Image")
    audit.add_argument("--dtb", type=Path, help="same-build X5 DTB")
    audit.add_argument("--kernel-modules", type=Path, help="module root or artifacts dir containing lib/modules")
    audit.add_argument("--force-decompress", action="store_true", help="ignore a cached decompressed image")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "audit":
        parser.print_help()
        return 2
    for name in ("kernel_config", "kernel_image", "dtb", "kernel_modules"):
        value = getattr(args, name)
        if value is not None and not value.exists():
            print(f"error: --{name.replace('_', '-')} does not exist: {value}", file=sys.stderr)
            return 2
    if args.image is not None and not args.image.exists():
        print(f"error: --image does not exist: {args.image}", file=sys.stderr)
        return 2
    if args.rootfs_dir is not None and not args.rootfs_dir.is_dir():
        print(f"error: --rootfs-dir is not a directory: {args.rootfs_dir}", file=sys.stderr)
        return 2

    auditor = Auditor(
        image=args.image or args.rootfs_dir,
        work_dir=args.work_dir,
        out_dir=args.out_json.parent,
        kernel_config=args.kernel_config,
        kernel_image=args.kernel_image,
        dtb=args.dtb,
        kernel_modules=args.kernel_modules,
        official_md5=args.official_md5,
        source_url=args.source_url,
        rootfs_dir=args.rootfs_dir,
    )
    try:
        doc = auditor.run()
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: audit failed: {exc}", file=sys.stderr)
        return 2

    _atomic_write(args.out_json, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    if args.out_md:
        _atomic_write(args.out_md, render(doc))
    p0 = sum(1 for f in doc.get("findings", []) if f["severity"] == "P0")
    p1 = sum(1 for f in doc.get("findings", []) if f["severity"] == "P1")
    gaps = len(doc.get("gaps", []))
    print(
        f"[audit] schema={SCHEMA} rootfs={doc.get('rootfs', {}).get('fstype')} "
        f"P0={p0} P1={p1} gaps={gaps} -> {args.out_json}",
        file=sys.stderr,
    )
    if doc.get("rootfs", {}).get("fstype") in (None, "fixture-none"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
