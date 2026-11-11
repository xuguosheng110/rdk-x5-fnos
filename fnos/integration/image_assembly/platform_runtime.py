"""Stage pinned X5 runtime files into an offline fnOS root filesystem.

This module does not configure accounts, networks, disks or fnOS application
data. Runtime presence is recorded separately from application acceptance.
"""
from __future__ import annotations

import hashlib
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import tarfile


SOURCES = {
    "x5-hobot-wifi": "01f38d74e0c90e81be00629ec921af05f25c8b33",
    "x5-hobot-multimedia": "0f297df905511c3429567202813359acee26dbb3",
    "x5-hobot-dnn": "b1db3ecac6bdb96070d62e104df6439167c26492",
}
WIFI_REQUIRED = (
    "fw_patch_table_8800d80_u02.bin", "fw_patch_8800d80_u02.bin",
    "fw_adid_8800d80_u02.bin", "fmacfw_8800d80_u02.bin",
    "aic_userconfig_8800d80.txt", "aic_powerlimit_8800d80.txt",
)
MM_LIBS = ("libmultimedia.so*", "libhbmem.so*", "libalog.so*", "libcnn_intf.so*")
BPU_MODULE_SHA256 = "b57a7d27cfcb2568ad0094818ad6cf8129fa72153c4aa9f02186329f4e0b859a"


def _destination(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe runtime path: {relative}")
    dst = root / relative
    parent = dst.parent
    while parent != root:
        if parent.is_symlink():
            raise ValueError(f"symlink in destination directory: {parent}")
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"non-directory runtime parent: {parent}")
        parent = parent.parent
    if dst.exists() and not dst.is_file() and not dst.is_symlink():
        raise ValueError(f"non-file runtime destination: {dst}")
    return dst


def _prepare_parent(root: Path, parent: Path) -> None:
    # System directories must not inherit the builder's group-writable umask.
    chain = []
    while parent != root:
        chain.append(parent)
        parent = parent.parent
    for path in reversed(chain):
        path.mkdir(exist_ok=True, mode=0o755)
        path.chmod(0o755)


def verify_runtime(root: Path, expected: dict, *, require_root_owner: bool = False) -> dict:
    """Verify every pinned payload/config from the mounted final filesystem."""
    root = root.resolve()
    manifest = _destination(root, Path("usr/share/fnos-x5/runtime-manifest.json"))
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("runtime manifest missing or replaced by a link")
    if json.loads(manifest.read_text()) != expected:
        raise ValueError("final runtime manifest differs from assembly input")
    for item in expected["files"]:
        path = _destination(root, Path(item["path"]))
        if "symlink" in item:
            if not path.is_symlink() or str(path.readlink()) != item["symlink"]:
                raise ValueError(f"incorrect final runtime link: {item['path']}")
        elif path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"incorrect final runtime file: {item['path']}")
    if require_root_owner:
        paths = [manifest, *(root / item["path"] for item in expected["files"])]
        for path in paths:
            while path != root:
                stat = path.lstat()
                if stat.st_uid != 0 or stat.st_gid != 0:
                    raise ValueError(f"non-root runtime ownership: {path.relative_to(root)}")
                if not path.is_symlink() and stat.st_mode & 0o022:
                    raise ValueError(f"writable runtime path: {path.relative_to(root)}")
                path = path.parent
    return {"verified_files": len(expected["files"]), "sources": expected["sources"]}


def create_validation_bundle(root: Path, report: dict, output: Path) -> None:
    """Export only reviewed payloads; never inherit the build account's IDs.

    This is an engineering runtime artifact, not an OS installer. Account,
    network, data-volume and host OS files are deliberately outside the plan.
    """
    verify_runtime(root, report)
    entries = {Path(x["path"]) for x in report["files"]}
    entries.add(Path("usr/share/fnos-x5/runtime-manifest.json"))
    for path in list(entries):
        entries.update(p for p in path.parents if p != Path("."))
    with output.open("xb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for relative in sorted(entries):
                src = _destination(root, relative) if relative in {Path(x["path"]) for x in report["files"]} else root / relative
                meta = archive.gettarinfo(str(src), arcname=str(relative))
                meta.uid = meta.gid = 0
                meta.uname = meta.gname = "root"
                meta.mtime = 0
                meta.mode = 0o755 if meta.isdir() else (0o777 if meta.issym() else 0o644)
                if meta.isfile():
                    with src.open("rb") as stream:
                        archive.addfile(meta, stream)
                else:
                    archive.addfile(meta)


def stage_runtime(root: Path, sources: Path, bpu_module: Path | None = None) -> dict:
    root, sources = root.resolve(), sources.resolve()
    if root == Path("/"):
        raise ValueError("offline rootfs required; refusing live host installation")
    if not (root / "etc/os-release").is_file():
        raise ValueError("destination is not a root filesystem staging directory")
    inputs = []
    # Verify all inputs before altering the output tree.
    for name, commit in SOURCES.items():
        repo = sources / name
        actual = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        if actual != commit:
            raise ValueError(f"unreviewed source revision: {name}: {actual}")
        if subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"], text=True).strip():
            raise ValueError(f"modified source tree: {name}")
    wifi = sources / "x5-hobot-wifi/debian/vendor/etc/firmware"
    for name in WIFI_REQUIRED:
        if not (wifi / name).is_file():
            raise ValueError(f"missing X5 firmware: {name}")
    inputs.extend((p, Path("vendor/etc/firmware") / p.name) for p in wifi.iterdir() if p.is_file())
    mm = sources / "x5-hobot-multimedia/debian/usr/hobot/lib"
    for pattern in MM_LIBS:
        matches = sorted(mm.glob(pattern))
        if not matches:
            raise ValueError(f"missing multimedia library: {pattern}")
        inputs.extend((p, Path("usr/hobot/lib") / p.name) for p in matches)
    firmware = mm / "firmware/libmm/chagall.bin"
    if not firmware.is_file():
        raise ValueError("missing X5 codec firmware")
    inputs.append((firmware, Path("usr/hobot/lib/firmware/libmm/chagall.bin")))
    for name in ("libdnn.so", "libhbrt_bayes_aarch64.so"):
        src = sources / "x5-hobot-dnn/x5/usr/lib" / name
        if not src.is_file():
            raise ValueError(f"missing BPU library: {name}")
        inputs.append((src, Path("usr/lib/hbbpu") / name))
    if bpu_module is not None:
        if not bpu_module.is_file() or hashlib.sha256(bpu_module.read_bytes()).hexdigest() != BPU_MODULE_SHA256:
            raise ValueError("unreviewed BPU hardware module")
        # Exact legacy 6.18.0 compatibility artifact validated on the Nov9 board.
        # This is explicitly a waiver, not an assertion of native source support.
        inputs.append((bpu_module, Path("usr/lib/modules/6.18.0/updates/bpu_hw_io_x5.ko")))
    configs = {
        "etc/ld.so.conf.d/fnos-x5-runtime.conf": "/usr/hobot/lib\n/usr/lib/hbbpu\n",
        "etc/modules-load.d/fnos-x5-wifi.conf": "# X5 SDIO Wi-Fi; firmware is bundled under /vendor/etc/firmware\naic8800_fdrv\n",
        "usr/lib/systemd/system/fnos-x5-runtime.service": (
            "[Unit]\nDescription=RDK X5 runtime library cache\nAfter=local-fs.target\n"
            "Before=mediasrv.service ai_manager.service trim_app_center.service\n"
            "[Service]\nType=oneshot\nExecStart=/sbin/ldconfig\nRemainAfterExit=yes\n"
            "[Install]\nWantedBy=multi-user.target\n"
        ),
    }
    if bpu_module is not None:
        configs["etc/modules-load.d/fnos-x5-bpu.conf"] = (
            "# Validated legacy hardware module; see runtime-manifest.json waiver\n"
            "bpu_framework\nbpu_cores\nbpu_hw_io_x5\n"
        )
    enable_path = Path("etc/systemd/system/multi-user.target.wants/fnos-x5-runtime.service")
    manifest_path = Path("usr/share/fnos-x5/runtime-manifest.json")
    # Validate the complete write plan before modifying even the first file.
    for src, relative in inputs:
        _destination(root, relative)
        if src.is_symlink():
            target = src.readlink()
            if target.is_absolute() or target.parent != Path(".") or not src.is_file():
                raise ValueError(f"unexpected library link: {src}")
    for path in [*map(Path, configs), enable_path, manifest_path]:
        dst = _destination(root, path)
        if path == enable_path:
            if dst.exists() and not dst.is_symlink():
                raise ValueError("runtime service enable path is not a symlink")
        elif dst.is_symlink():
            raise ValueError(f"runtime config destination is a symlink: {path}")
    inventory = []
    for src, relative in inputs:
        dst = root / relative
        _prepare_parent(root, dst.parent)
        if dst.is_symlink() or (src.is_symlink() and dst.exists()):
            dst.unlink()
        if src.is_symlink():
            dst.symlink_to(src.readlink())
            inventory.append({"path": str(relative), "symlink": str(src.readlink())})
        else:
            shutil.copyfile(src, dst)
            dst.chmod(0o644)
            inventory.append({"path": str(relative), "sha256": hashlib.sha256(dst.read_bytes()).hexdigest()})
    for path, contents in configs.items():
        dst = root / path
        _prepare_parent(root, dst.parent)
        dst.write_text(contents)
        dst.chmod(0o644)
        inventory.append({"path": path, "sha256": hashlib.sha256(dst.read_bytes()).hexdigest()})
    link = root / enable_path
    _prepare_parent(root, link.parent)
    if link.is_symlink():
        link.unlink()
    link.symlink_to("/usr/lib/systemd/system/fnos-x5-runtime.service")
    inventory.append({"path": str(enable_path), "symlink": str(link.readlink())})
    report = {
        "schema": "fnos-x5-runtime/v1", "sources": SOURCES,
        "files": inventory, "application_acceptance": "not implied by runtime installation",
        "pending": ["household storage native workflow acceptance", "fnOS media backend", "fnOS photos BPU backend"],
        "bpu_hardware_module": ({"sha256": BPU_MODULE_SHA256,
                                 "acceptance": "PASS-WAIVER: legacy 6.18.0 hardware module; not native source support"}
                                if bpu_module is not None else "not bundled"),
    }
    dest = root / manifest_path
    _prepare_parent(root, dest.parent)
    dest.write_text(json.dumps(report, indent=2) + "\n")
    dest.chmod(0o644)
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--rootfs", type=Path, required=True)
    ap.add_argument("--sources", type=Path, required=True)
    ap.add_argument("--bpu-module", type=Path)
    ap.add_argument("--validation-bundle", type=Path, help="export reviewed files with root ownership; not a full OS installer")
    args = ap.parse_args()
    result = stage_runtime(args.rootfs, args.sources, args.bpu_module)
    if args.validation_bundle:
        create_validation_bundle(args.rootfs, result, args.validation_bundle)
    print(json.dumps({"files": len(result["files"]), "sources": result["sources"]}))
