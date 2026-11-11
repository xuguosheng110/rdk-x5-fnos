"""Offline X5 + fnOS candidate image assembly.

Run this script under ``fakeroot``.  fakeroot is what lets the unprivileged
build user preserve uid/gid/mode/xattrs while ``btrfs restore`` and
``mke2fs -d`` run; no mount and no sudo are used.

The build is intentionally conservative:

* the target rootfs is ext4 (the X5 boot.cmd/root layout is ext4), populated
  with the audited fnOS userspace;
* fnOS 6.18.18 modules are removed and the same-build X5 6.18.0 modules are
  installed;
* a minimal AArch64 initramfs carries the X5 modules and busybox;
* the source fnOS boot chain is replaced, not copied;
* first-boot resize and updatemgr are disabled by systemd conditions.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from fnos.integration.input_audit.image import parse_partitions, probe_filesystem
from .mbr import build_mbr, parse_mbr
from .platform_runtime import stage_runtime, verify_runtime
from .home_storage import stage_home_storage, verify_home_storage, UNITS as HOME_STORAGE_UNITS
from .native_media import stage_native_media, verify_native_media
from .native_photos import stage_native_photos, verify_native_photos
from .native_semantic768 import stage_native_semantic768, verify_native_semantic768
from .product_inputs import stage_product_inputs, verify_product_inputs

MIB = 1024 * 1024
GIB = 1024 * 1024 * 1024
P1_START_LBA = 8192
P1_SECTORS = 524288  # 256 MiB
P2_START_LBA = 532480  # 260 MiB, same as the X5 base image
SEC = 512
# X5 U-Boot defaults recovered from validation/linux-6.18-rdk-x5/recovery/
# product_20260408/uboot.img: kernel_addr_r=0x85800000, fdt_addr_r=0x84000000.
# ramdisk_addr_r is explicit and placed above every enabled reserved-memory
# node in the selected x5-rdk-v1p0 DTB (CMA ends 0x9fe00000, ADSP ends
# 0xa2080000, ramoops ends 0xa40c0000; initrd starts 0xc0000000).
KERNEL_LOAD = 0x85800000
FDT_LOAD = 0x84000000
INITRD_LOAD = 0xC0000000
DRAM_START = 0x84000000
DRAM_END = 0x280000000  # 8 GiB from 0x80000000
INITRD_MAX = 0x20000000  # 512 MiB hard ceiling for this candidate
LIVE_UBOOT_LMB = (0x8BE990D0, 0x90000000)  # live bdinfo reserved[0]
PRODUCT_KERNEL_OPTIONS = {
    # fnOS mdadm requests lockless bitmaps even for single-disk Basic pools.
    # Mounting an existing pool does not establish this creation capability.
    'CONFIG_BLK_DEV_MD': {'y', 'm'},
    'CONFIG_MD_LLBITMAP': {'y'},
    'CONFIG_MD_RAID1': {'y', 'm'},
    'CONFIG_PCI': {'y'},
    'CONFIG_PROC_FS': {'y'},
    'CONFIG_NF_TABLES': {'y', 'm'},
    'CONFIG_NF_TABLES_INET': {'y'},
    'CONFIG_NF_TABLES_IPV4': {'y'},
    'CONFIG_NF_TABLES_IPV6': {'y'},
    'CONFIG_NFT_NAT': {'y', 'm'},
    'CONFIG_NFT_MASQ': {'y', 'm'},
    'CONFIG_ZRAM': {'y', 'm'},
    'CONFIG_ZRAM_BACKEND_LZ4': {'y'},
    'CONFIG_ZSMALLOC': {'y', 'm'},
}
MASK_UNITS = (
    "resize-rootfs.service",
    "updatemgr.service",
    "apt-daily.timer",
    "apt-daily-upgrade.timer",
    "unattended-upgrades.service",
    "auto_update_miniboot.service",
)


def log(msg: str) -> None:
    print(f"[assemble] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 3600) -> subprocess.CompletedProcess:
    log("run: " + " ".join(str(x) for x in cmd))
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\nSTDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-4000:]}")
    return proc


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * MIB), b""):
            h.update(block)
    return h.hexdigest()


def validate_product_kernel(config: Path) -> dict:
    """Catch observed native storage, sysinfo and Docker blockers before restore."""
    values = dict(line.split('=', 1) for line in config.read_text().splitlines()
                  if line.startswith('CONFIG_') and '=' in line)
    missing = [name for name, allowed in PRODUCT_KERNEL_OPTIONS.items()
               if values.get(name) not in allowed]
    if missing:
        raise ValueError('kernel lacks native product requirements: ' + ', '.join(missing))
    return {name: values[name] for name in PRODUCT_KERNEL_OPTIONS}


def chown_root(path: Path) -> None:
    run(["chown", "-R", "0:0", str(path)])


def write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    run(["chown", "0:0", str(path)])


def copy_file(src: Path, dst: Path, mode: int | None = None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    if mode is not None:
        os.chmod(dst, mode)
    run(["chown", "0:0", str(dst)])


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    run(["cp", "-a", str(src), str(dst)])


def restore_rootfs(part: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    btrfs = shutil.which("btrfs")
    if not btrfs:
        raise RuntimeError("btrfs not in PATH; use the private btrfs-progs")
    proc = run([btrfs, "restore", "-i", "-S", "-m", "-x", str(part), str(dest)], timeout=1800)
    if proc.stderr.strip():
        log("btrfs restore stderr tail:\n" + "\n".join(proc.stderr.splitlines()[-20:]))
    for required in ("etc/os-release", "bin/busybox", "usr/trim/bin/trim"):
        if not (dest / required).exists():
            raise RuntimeError(f"rootfs restore incomplete: missing {required}")


def adapt_boot_cmd(text: str) -> str:
    """Load a raw gzip initrd with the U-Boot booti address:size form."""
    if 'imagefile="Image"' not in text:
        raise ValueError("base boot.cmd has no imagefile=Image")
    text = text.replace(
        'imagefile="Image"',
        'imagefile="Image"\n'
        'initrdfile="initrd.img-6.18.0"\n'
        'setenv kernel_addr_r 0x85800000\n'
        'setenv fdt_addr_r 0x84000000\n'
        'setenv ramdisk_addr_r 0xc0000000\n'
        'setenv initrd_high 0xffffffff\n'
        'setenv bootm_low 0x84000000\n'
        'setenv bootm_size 0x100000000',
        1,
    )
    old = "booti ${kernel_addr_r} - ${fdt_addr_r}"
    if old not in text:
        raise ValueError("base boot.cmd has no expected booti line")
    block = "\n".join([
        'initrd_load_addr="-"',
        'if test -n "${initrdfile}"; then',
        '  if ext4load ${devtype} ${devnum}:${devplist} ${ramdisk_addr_r} ${prefix}${initrdfile}; then',
        '    setenv initrd_size 0x${filesize}',
        '    if test -z "${initrd_size}"; then',
        '      echo "ERROR: initrd size is empty"; exit',
        '    fi',
        '    if test ${initrd_size} -le 0; then',
        '      echo "ERROR: initrd size is zero"; exit',
        '    fi',
        '    if test ${initrd_size} -gt 0x20000000; then',
        '      echo "ERROR: initrd exceeds 512 MiB"; exit',
        '    fi',
        '    setenv initrd_load_addr "${ramdisk_addr_r}:${initrd_size}"',
        '  else',
        '    echo "ERROR: cannot load ${prefix}${initrdfile}"; exit',
        '  fi',
        'fi',
        'booti ${kernel_addr_r} ${initrd_load_addr} ${fdt_addr_r}',
    ])
    return text.replace(old, block, 1)


def harden_boot_cmd(text: str) -> str:
    """Add checked FDT/kernel loads without U-Boot's unsupported ':' builtin."""
    text = adapt_boot_cmd(text)
    text = text.replace(
        "ext4load ${devtype} ${devnum}:${devplist} ${fdt_addr_r} ${prefix}hobot/${fdtfile}",
        "if ext4load ${devtype} ${devnum}:${devplist} ${fdt_addr_r} ${prefix}hobot/${fdtfile}; then echo \"Loaded FDT\"; else echo \"ERROR: cannot load FDT\"; exit; fi",
        1,
    )
    text = text.replace(
        "ext4load ${devtype} ${devnum}:${devplist} ${kernel_addr_r} ${prefix}${imagefile}",
        "if ext4load ${devtype} ${devnum}:${devplist} ${kernel_addr_r} ${prefix}${imagefile}; then echo \"Loaded kernel\"; else echo \"ERROR: cannot load kernel\"; exit; fi",
        1,
    )
    if "then :" in text or "if :" in text:
        raise ValueError("unsupported ':' command remains in boot.cmd")
    return text


def mask_boot_affecting_units(root: Path) -> dict:
    """Mask update/resize units; preserve source units under /etc/fnos-x5."""
    systemd = root / "etc/systemd/system"
    disabled = root / "etc/fnos-x5/disabled-units"
    disabled.mkdir(parents=True, exist_ok=True)
    masked = []
    preserved = []
    for unit in ("resize-rootfs.service", "updatemgr.service"):
        d = systemd / f"{unit}.d"
        if d.exists():
            shutil.rmtree(d)
    for unit in MASK_UNITS:
        dst = systemd / unit
        if dst.is_symlink() and os.readlink(dst) == "/dev/null":
            masked.append(unit)
            continue
        if dst.exists() or dst.is_symlink():
            if dst.is_symlink():
                dst.unlink()
            elif dst.is_file():
                saved = disabled / unit
                if saved.exists():
                    saved.unlink()
                shutil.copy2(dst, saved)
                run(["chown", "0:0", str(saved)])
                dst.unlink()
                preserved.append(str(saved.relative_to(root)))
            else:
                raise RuntimeError(f"unexpected unit path type: {dst}")
        os.symlink("/dev/null", dst)
        masked.append(unit)
    removed = []
    for dirpath, _dirnames, filenames in os.walk(systemd):
        for name in filenames:
            p = Path(dirpath) / name
            if not p.is_symlink():
                continue
            if p.parent == systemd and name in MASK_UNITS:
                continue  # never delete the /dev/null mask itself
            low = name.lower()
            if any(k in low for k in ("resize-rootfs", "updatemgr", "apt-daily", "unattended-upgrades", "auto_update_miniboot")):
                p.unlink()
                removed.append(str(p.relative_to(root)))
    write_text(
        disabled / "README.txt",
        "These units are masked by /etc/systemd/system/<unit> -> /dev/null.\n"
        "Original unit files, when present in /etc, are preserved in this directory.\n"
        "Default is deny; there is no allow file. Core fnOS services are untouched.\n",
    )
    return {"masked_units": sorted(set(masked)), "preserved_units": sorted(preserved), "removed_enabled_links": sorted(removed)}


def prepare_rootfs(
    root: Path,
    x5: Path,
    boot_cmd_src: Path,
    source_image_sha: str,
    rootfs_uuid: str,
) -> dict:
    """Mutate the metadata-preserving staging tree into the X5 userspace."""
    lost = root / "lost+found"
    if lost.exists():
        shutil.rmtree(lost)

    # 1) Remove the fnOS target kernel modules; install same-build X5 modules.
    modules_root = root / "lib" / "modules"
    for child in list(modules_root.iterdir()):
        if child.is_dir() or child.is_symlink():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    src_modules = x5 / "modules-root" / "lib" / "modules" / "6.18.0"
    if not src_modules.is_dir():
        raise RuntimeError(f"missing X5 modules: {src_modules}")
    copy_tree(src_modules, modules_root / "6.18.0")
    for junk in ("build", "source"):
        p = modules_root / "6.18.0" / junk
        if p.is_symlink() or p.exists():
            p.unlink()
    chown_root(modules_root / "6.18.0")
    depmod = shutil.which("depmod")
    if not depmod:
        raise RuntimeError("depmod not installed")
    proc = run([depmod, "-b", str(root), "6.18.0"], timeout=1200)
    if "not found" in (proc.stderr + proc.stdout).lower():
        log("depmod reported missing modules:\n" + (proc.stderr + proc.stdout)[-3000:])

    # 2) Replace fnOS boot files with X5 boot files, but keep /boot as a directory.
    boot = root / "boot"
    if boot.exists():
        for child in list(boot.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        boot.mkdir()
    hobot = boot / "hobot"
    hobot.mkdir()
    copy_file(x5 / "Image-6.18.0", boot / "Image", 0o644)
    copy_file(x5 / "config-6.18.0", boot / "config-6.18.0", 0o644)
    if (x5 / "System.map-6.18.0").exists():
        copy_file(x5 / "System.map-6.18.0", boot / "System.map-6.18.0", 0o644)
    dtbs = sorted((x5 / "dtbs").glob("*.dtb"))
    if not dtbs:
        raise RuntimeError("no X5 DTBs found")
    for dtb in dtbs:
        copy_file(dtb, hobot / dtb.name, 0o644)
    if not (hobot / "x5-rdk.dtb").exists():
        raise RuntimeError("x5-rdk.dtb missing from X5 artifacts")

    # 3) Adapt the real X5 boot.cmd and build boot.scr.
    boot_cmd = harden_boot_cmd(boot_cmd_src.read_text(encoding="utf-8", errors="replace"))
    write_text(boot / "boot.cmd", boot_cmd, 0o755)
    mkimage = shutil.which("mkimage")
    if not mkimage:
        raise RuntimeError("mkimage not in PATH (private u-boot-tools)")
    run([mkimage, "-C", "none", "-A", "arm", "-T", "script", "-d", str(boot / "boot.cmd"), str(boot / "boot.scr")])
    os.chmod(boot / "boot.scr", 0o644)
    if not (boot / "boot.scr").is_file():
        raise RuntimeError("boot.scr was not generated")

    # 4) fstab: new ext4 UUID, no source /boot entry.
    fstab = (
        "# Generated by fnos/integration/image_assembly; source fstab archived below.\n"
        f"UUID={rootfs_uuid} / ext4 defaults,noatime 0 1\n"
        "tmpfs /tmp tmpfs defaults,nosuid 0 0\n"
    )
    write_text(root / "etc/fstab", fstab, 0o644)
    source_fstab = root / "etc" / "fstab"
    # source fstab is gone; record the values in the source identity file below.
    write_text(
        root / "etc/fnos-x5/source-fstab.txt",
        "Source fnOS R3S fstab (audit record; not active):\n"
        "UUID=cd673f50-2a05-427e-889f-6d259ae19207 / btrfs defaults,noatime 0 1\n"
        "UUID=1dd9176d-8069-4267-831d-61555f4b9653 /boot ext4 defaults,noatime,errors=remount-ro 0 2\n"
        "tmpfs /tmp tmpfs defaults,nosuid 0 0\n",
    )

    # 5) Explicit X5 board identity; keep the source identity separately.
    identity = {
        "boot_board": "x5-rdk\n",
        "boot_brand": "d-robotics\n",
        "boot_family": "sunrise5\n",
        "boot_mode": "uboot\n",
        "platform": "arm\n",
        "edition": "Mainland-PE\n",
    }
    for name, value in identity.items():
        write_text(root / "etc/device_info" / name, value)
    write_text(
        root / "etc/fnos-x5/source-identity.txt",
        "source_image=fnOS ARM 1.2.0701 友善 NanoPi R3S\n"
        "source_boot_board=nanopi-r3s\n"
        "source_boot_brand=rockchip\n"
        "source_boot_family=rk35xx\n"
        "source_boot_mode=uboot\n"
        "source_platform=arm\n"
        f"source_image_sha256={source_image_sha}\n",
    )
    write_text(
        root / "etc/fnos-x5/README.txt",
        "X5 adaptation guard files.\n"
        "allow-auto-resize/allow-updatemgr do not exist: first-boot resize and\n"
        "updatemgr are disabled by systemd ConditionPathExists drop-ins.\n"
        "Source identity is kept in source-identity.txt; this is not a Rockchip board.\n",
    )

    # 6) Default-deny masks for update/resize; preserve the original units.
    unit_state = mask_boot_affecting_units(root)

    # 7) Keep the source hostname and userspace; only the board identity changes.
    return {
        "modules_release": "6.18.0",
        "masked_units": unit_state["masked_units"],
        "preserved_units": unit_state["preserved_units"],
        "removed_enabled_links": unit_state["removed_enabled_links"],
        "fstab": fstab,
        "device_info": {k: v.strip() for k, v in identity.items()},
    }


def build_initramfs(root: Path, x5: Path, work: Path, mkimage: str | None = None) -> Path:
    idir = work / "initramfs"
    if idir.exists():
        shutil.rmtree(idir)
    idir.mkdir(parents=True)
    (idir / "bin").mkdir()
    (idir / "sbin").mkdir()
    (idir / "lib" / "aarch64-linux-gnu").mkdir(parents=True)
    for d in ("proc", "sys", "dev", "root", "tmp"):
        (idir / d).mkdir()
    copy_file(root / "bin" / "busybox", idir / "bin" / "busybox", 0o755)
    copy_file(root / "lib" / "aarch64-linux-gnu" / "ld-linux-aarch64.so.1", idir / "lib" / "aarch64-linux-gnu" / "ld-linux-aarch64.so.1", 0o755)
    copy_file(root / "lib" / "aarch64-linux-gnu" / "libc.so.6", idir / "lib" / "aarch64-linux-gnu" / "libc.so.6", 0o755)
    copy_file(root / "lib" / "aarch64-linux-gnu" / "libresolv.so.2", idir / "lib" / "aarch64-linux-gnu" / "libresolv.so.2", 0o755)
    os.symlink("/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1", idir / "lib" / "ld-linux-aarch64.so.1")
    for name in ("sh", "mount", "umount", "cat", "grep", "sed", "sleep", "mknod", "ln", "mkdir", "ls"):
        os.symlink("/bin/busybox", idir / "bin" / name)
    for name in ("modprobe", "switch_root"):
        os.symlink("/bin/busybox", idir / "sbin" / name)
    modules_dst = idir / "lib" / "modules"
    modules_dst.mkdir(parents=True)
    copy_tree(x5 / "modules-root" / "lib" / "modules" / "6.18.0", modules_dst / "6.18.0")
    init = """#!/bin/sh
export PATH=/bin:/sbin
mkdir -p /proc /sys /dev /root /tmp
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
for m in ext4 btrfs dm_mod dm_crypt xts usb_storage uas sd_mod xhci_hcd mmc_block; do
    modprobe "$m" 2>/dev/null || true
done
ROOT=""
FSTYPE="ext4"
for a in $(cat /proc/cmdline); do
    case "$a" in
        root=*) ROOT="${a#root=}" ;;
        rootfstype=*) FSTYPE="${a#rootfstype=}" ;;
    esac
done
[ -n "$ROOT" ] || ROOT=/dev/mmcblk0p2
i=0
while [ ! -b "$ROOT" ] && [ "$i" -lt 30 ]; do
    sleep 1
    i=$((i + 1))
done
if ! mount -t "$FSTYPE" -o rw,noatime "$ROOT" /root; then
    echo "fnos-x5: cannot mount $ROOT ($FSTYPE); dropping to shell"
    exec /bin/sh
fi
exec /sbin/switch_root /root /sbin/init
"""
    write_text(idir / "init", init, 0o755)
    chown_root(idir)
    initrd = work / "initrd.img-6.18.0"
    # cpio is run under the same fakeroot session, preserving root ownership.
    run(
        ["bash", "-c", "find . -print0 | cpio --null -o --format=newc | gzip -9 > " + str(initrd)],
        cwd=idir,
        timeout=1800,
    )
    os.chmod(initrd, 0o644)
    run(["gzip", "-t", str(initrd)])
    return initrd


def build_ext4(root: Path, dest: Path, size_bytes: int, fs_uuid: str) -> dict:
    if size_bytes % SEC:
        raise ValueError("rootfs size must be sector aligned")
    with open(dest, "wb") as f:
        f.truncate(size_bytes)
    mke2fs = shutil.which("mke2fs")
    if not mke2fs:
        raise RuntimeError("mke2fs not installed")
    run(
        [mke2fs, "-q", "-t", "ext4", "-F", "-U", fs_uuid, "-L", "fnos-x5-rootfs", "-m", "1", "-d", str(root), str(dest)],
        timeout=1800,
    )
    dumpe2fs = shutil.which("dumpe2fs")
    info = run([dumpe2fs, "-h", str(dest)], timeout=120).stdout
    got = None
    for line in info.splitlines():
        if line.startswith("Filesystem UUID:"):
            got = line.split(":", 1)[1].strip()
    if got != fs_uuid:
        raise RuntimeError(f"ext4 UUID mismatch: {got} != {fs_uuid}")
    run(["e2fsck", "-fn", str(dest)], timeout=600)
    return {"path": str(dest), "size": dest.stat().st_size, "sha256": sha256(dest), "uuid": got}


def build_fat(dest: Path, size_bytes: int = 256 * MIB) -> dict:
    with open(dest, "wb") as f:
        f.truncate(size_bytes)
    run(["mkfs.vfat", "-F", "32", "-n", "CONFIG", str(dest)], timeout=120)
    return {"path": str(dest), "size": dest.stat().st_size, "sha256": sha256(dest)}


def copy_sparse_partition(source, destination, offset: int) -> int:
    """Copy into a fresh zero-filled image, preserving source holes.

    Skipping holes is valid only because assemble_image creates a new blank
    file first. Fall back to zero-block skipping on filesystems without
    SEEK_DATA/SEEK_HOLE. Return actual bytes written, not logical file size.
    """
    size = os.fstat(source.fileno()).st_size
    written = 0

    def copy_range(start, end):
        nonlocal written
        source.seek(start)
        position = start
        while position < end:
            block = source.read(min(8 * MIB, end - position))
            if not block:
                raise RuntimeError('partition changed during image assembly')
            if block.count(0) != len(block):
                destination.seek(offset + position)
                destination.write(block)
                written += len(block)
            position += len(block)

    if not hasattr(os, 'SEEK_DATA') or not hasattr(os, 'SEEK_HOLE'):
        copy_range(0, size)
        return written
    position = 0
    while position < size:
        try:
            start = os.lseek(source.fileno(), position, os.SEEK_DATA)
            end = min(os.lseek(source.fileno(), start, os.SEEK_HOLE), size)
        except OSError as error:
            if error.errno == errno.ENXIO:
                break
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
            copy_range(position, size)
            break
        if not position <= start < end:
            raise RuntimeError('invalid partition extent during image assembly')
        copy_range(start, end)
        position = end
    return written


def assemble_image(mbr_base: Path, p1: Path, p2: Path, out: Path) -> dict:
    if out.resolve() in {p.resolve() for p in (mbr_base, p1, p2)}:
        raise ValueError('output must not replace an input')
    base = mbr_base.read_bytes()
    p2_size = p2.stat().st_size
    p2_sectors = p2_size // SEC
    total_sectors = P2_START_LBA + p2_sectors
    total = total_sectors * SEC
    log(f"writing sparse image: {total} bytes")
    with open(out, "wb") as f:
        f.truncate(total)
    mbr = build_mbr(base, P1_START_LBA, P1_SECTORS, P2_START_LBA, p2_sectors)
    with open(out, "r+b") as f:
        f.seek(0)
        f.write(mbr)
    for src, off in ((p1, P1_START_LBA * SEC), (p2, P2_START_LBA * SEC)):
        with open(src, "rb") as s, open(out, "r+b") as d:
            written = copy_sparse_partition(s, d, off)
            log(f"partition {src.name}: logical={src.stat().st_size}, written={written}")
    return {
        "path": str(out),
        "size": total,
        "sha256": sha256(out),
        "mbr": parse_mbr(mbr),
        "p1": {"start": P1_START_LBA * SEC, "sectors": P1_SECTORS},
        "p2": {"start": P2_START_LBA * SEC, "sectors": p2_sectors},
    }


def debugfs_read(p2: Path, path: str) -> str:
    proc = run(["debugfs", "-R", f'cat "{path}"', str(p2)], timeout=60)
    return proc.stdout


def debugfs_ls(p2: Path, path: str) -> list[str]:
    proc = run(["debugfs", "-R", f'ls -p "{path}"', str(p2)], timeout=60)
    names = []
    for line in proc.stdout.splitlines():
        # debugfs -p examples:
        # /14/040755/0/0/.//
        # /15/100644/0/0/Image/23076872/
        parts = line.strip().strip("/").split("/")
        if len(parts) >= 5:
            name = parts[4]
            if name not in (".", ".."):
                names.append(name)
    return names


def verify(root: Path, x5: Path, p1: Path, p2: Path, out: Path, initrd: Path, fs_uuid: str, output_sha256: str | None = None) -> dict:
    checks: dict[str, object] = {}
    parsed = parse_partitions(out)
    checks["mbr_table"] = parsed["table"]
    checks["partition_count"] = len(parsed["partitions"])
    fs1 = probe_filesystem(out, P1_START_LBA * SEC, P1_SECTORS * SEC)
    fs2 = probe_filesystem(out, P2_START_LBA * SEC, p2.stat().st_size)
    checks["p1_fs"] = fs1["type"]
    checks["p2_fs"] = fs2["type"]
    checks["p2_uuid"] = fs2.get("uuid")
    checks["uuid_match"] = fs2.get("uuid") == fs_uuid
    fstab = debugfs_read(p2, "/etc/fstab")
    checks["fstab_has_uuid"] = f"UUID={fs_uuid}" in fstab
    checks["fstab_no_source_uuid"] = "cd673f50-2a05-427e-889f-6d259ae19207" not in fstab
    modules = debugfs_ls(p2, "/lib/modules")
    checks["modules"] = modules
    checks["modules_only_6180"] = modules == ["6.18.0"]
    boot = debugfs_ls(p2, "/boot")
    checks["boot_files"] = boot
    checks["boot_has_image"] = "Image" in boot
    checks["boot_has_initrd"] = "initrd.img-6.18.0" in boot
    checks["boot_has_scr"] = "boot.scr" in boot
    checks["boot_has_dtb"] = "hobot" in debugfs_ls(p2, "/boot")
    identity = debugfs_read(p2, "/etc/fnos-x5/source-identity.txt")
    checks["source_identity_kept"] = "nanopi-r3s" in identity
    device_info = debugfs_read(p2, "/etc/device_info/boot_board")
    checks["x5_identity"] = device_info.strip()
    stat = run(["debugfs", "-R", "stat /bin/busybox", str(p2)], timeout=60).stdout
    checks["busybox_uid0"] = re.search(r"User:\s+0\s+Group:\s+0", stat) is not None
    # architecture of the init binary
    with tempfile.TemporaryDirectory() as td:
        bb = Path(td) / "busybox"
        run(["debugfs", "-R", f'dump /bin/busybox {bb}', str(p2)], timeout=60)
        checks["busybox_arch"] = run(["readelf", "-h", str(bb)], timeout=60).stdout
        checks["busybox_aarch64"] = "AArch64" in checks["busybox_arch"]
    checks["initrd_gzip"] = run(["gzip", "-t", str(initrd)], timeout=120).returncode == 0
    listing = run(["bash", "-c", "gzip -dc " + str(initrd) + " | cpio -it 2>/dev/null"], timeout=300).stdout
    checks["initrd_init"] = "init" in listing.splitlines()
    checks["initrd_busybox"] = "bin/busybox" in listing.splitlines()
    checks["initrd_btrfs"] = "lib/modules/6.18.0/kernel/fs/btrfs/btrfs.ko" in listing.splitlines()
    checks["initrd_mountpoints"] = all(d in listing.splitlines() for d in ("proc", "sys", "dev", "root", "tmp"))
    checks["boot_scr_image"] = "Linux Script" in run(["mkimage", "-l", str(debugfs_dump(p2, "/boot/boot.scr"))], timeout=60).stdout
    checks["image_arch"] = run(["file", str(x5 / "Image-6.18.0")], timeout=30).stdout
    checks["image_arm64"] = "ARM64" in checks["image_arch"] or "aarch64" in checks["image_arch"]
    checks["output_sha256"] = output_sha256 or sha256(out)
    checks["output_size"] = out.stat().st_size
    checks["all_pass"] = all(
        bool(checks[k])
        for k in (
            "uuid_match", "fstab_has_uuid", "fstab_no_source_uuid", "modules_only_6180",
            "boot_has_image", "boot_has_initrd", "boot_has_scr", "boot_has_dtb",
            "source_identity_kept", "busybox_uid0", "busybox_aarch64", "initrd_gzip",
            "initrd_init", "initrd_busybox", "initrd_btrfs", "initrd_mountpoints", "boot_scr_image", "image_arm64",
        )
    )
    return checks


def debugfs_dump(p2: Path, path: str) -> Path:
    out = Path(tempfile.mkdtemp(prefix="fnos-x5-dump-")) / Path(path).name
    run(["debugfs", "-R", f'dump "{path}" {out}', str(p2)], timeout=60)
    return out


def ext4_uuid_from_image(image: Path, offset: int) -> str:
    with open(image, "rb") as f:
        f.seek(offset + 1024)
        sb = f.read(1024)
    if len(sb) < 256 or sb[0x38:0x3A] != b"\x53\xef":
        raise RuntimeError("final p2 ext4 superblock magic not found")
    return str(uuid.UUID(bytes=sb[0x68:0x78]))


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return default


def _os_release(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def _dpkg_version(status: str, package: str) -> dict:
    blocks = status.split("\n\n")
    for block in blocks:
        if f"Package: {package}\n" in block + "\n":
            fields = {}
            for line in block.splitlines():
                if ": " in line:
                    k, v = line.split(": ", 1)
                    fields[k] = v
            if fields.get("Package") == package:
                return fields
    return {}


def _systemctl_root(mnt: Path, unit: str) -> str:
    try:
        proc = subprocess.run(
            ["systemctl", f"--root={mnt}", "is-enabled", unit],
            capture_output=True, text=True, timeout=30
        )
        return f"rc={proc.returncode} out={(proc.stdout + proc.stderr).strip()}"
    except Exception as exc:
        return f"ERROR {exc}"


def sha256_image_regions(image: Path, p1_start: int, p1_size: int, p2_start: int, p2_size: int) -> tuple[str, str, str]:
    hf = hashlib.sha256(); h1 = hashlib.sha256(); h2 = hashlib.sha256()
    pos = 0
    with open(image, "rb") as f:
        while True:
            b = f.read(8 * MIB)
            if not b:
                break
            hf.update(b)
            a, z = pos, pos + len(b)
            lo, hi = max(a, p1_start), min(z, p1_start + p1_size)
            if lo < hi:
                h1.update(b[lo-a:hi-a])
            lo, hi = max(a, p2_start), min(z, p2_start + p2_size)
            if lo < hi:
                h2.update(b[lo-a:hi-a])
            pos = z
    return hf.hexdigest(), h1.hexdigest(), h2.hexdigest()


def parse_reserved_memory(dtb: Path) -> list[dict]:
    """Read enabled reserved-memory ranges from the selected final DTB."""
    fdtget = shutil.which("fdtget")
    if not fdtget:
        raise RuntimeError("fdtget not in PATH")
    listing = run([fdtget, "-l", str(dtb), "/reserved-memory"], timeout=30).stdout
    out = []
    for node in listing.split():
        status = ""
        proc = subprocess.run([fdtget, "-t", "s", str(dtb), f"/reserved-memory/{node}", "status"], capture_output=True, text=True)
        if proc.returncode == 0:
            status = proc.stdout.strip()
        if status == "disabled":
            continue
        reg = run([fdtget, "-t", "x", str(dtb), f"/reserved-memory/{node}", "reg"], timeout=30).stdout.split()
        if len(reg) < 4:
            continue
        hi, lo, shi, slo = (int(x, 16) for x in reg[:4])
        start = (hi << 32) | lo
        size = (shi << 32) | slo
        out.append({"node": node, "start": start, "end": start + size, "size": size, "status": status or "okay"})
    return out


def verify_final(image: Path, work: Path, expected_p1_sha256: str | None = None, expected_p2_sha256: str | None = None, expected_runtime: dict | None = None, expected_home_storage: dict | None = None, expected_native_media: dict | None = None, expected_native_photos: dict | None = None, expected_product_inputs: dict | None = None, expected_native_semantic768: dict | None = None) -> dict:
    """All critical checks read the final image, not staging.

    The p2 filesystem is mounted read-only through private-image fuse2fs at the
    exact MBR offset.  Every path below is under that mount.
    """
    if not image.is_file():
        raise RuntimeError(f"final image missing: {image}")
    parsed = parse_partitions(image)
    if parsed["table"] != "mbr" or len(parsed["partitions"]) != 2:
        raise RuntimeError("final image is not the expected two-partition MBR")
    p1, p2 = parsed["partitions"]
    if p1["start_bytes"] != P1_START_LBA * SEC or p2["start_bytes"] != P2_START_LBA * SEC:
        raise RuntimeError("final partition offsets do not match constants")
    final_sha, p1_sha, p2_sha = sha256_image_regions(image, P1_START_LBA * SEC, P1_SECTORS * SEC, P2_START_LBA * SEC, p2["size_bytes"])
    fs_uuid = ext4_uuid_from_image(image, P2_START_LBA * SEC)
    fs1 = probe_filesystem(image, P1_START_LBA * SEC, P1_SECTORS * SEC)
    fs2 = probe_filesystem(image, P2_START_LBA * SEC, p2["size_bytes"])
    # FUSE mountpoints under /data are rejected by the server's mount policy;
    # use a private /tmp directory (the image itself remains on /data).
    mount = Path(tempfile.mkdtemp(prefix="fnos-x5-final-mnt-", dir="/tmp"))
    fuse2fs = shutil.which("fuse2fs")
    if not fuse2fs:
        raise RuntimeError("fuse2fs not in PATH")
    mounted = False
    try:
        # fuse2fs fsyncs the image even for a read-only mount. A freshly
        # assembled multi-GiB image on the build server's USB HDD can take
        # several minutes to flush; do not abort that required durability step.
        run([fuse2fs, "-o", f"ro,offset={P2_START_LBA * SEC}", str(image), str(mount)], timeout=900)
        mounted = True
        time.sleep(1)
        fstab = _read(mount / "etc/fstab")
        osrel = _os_release(_read(mount / "etc/os-release"))
        trim_ver = _read(mount / "usr/trim/etc/version").strip()
        dpkg_trim = _dpkg_version(_read(mount / "var/lib/dpkg/status"), "trim")
        modules = sorted(x.name for x in (mount / "lib/modules").iterdir() if x.is_dir())
        boot_names = sorted(x.name for x in (mount / "boot").iterdir())
        dtbs = sorted(x.name for x in (mount / "boot/hobot").glob("*.dtb"))
        selected_dtb = mount / "boot/hobot/x5-rdk-v1p0.dtb"
        selected_dtb_sha = sha256(selected_dtb)
        reservations = parse_reserved_memory(selected_dtb)
        image_size = (mount / "boot/Image").stat().st_size
        fdt_size = selected_dtb.stat().st_size
        initrd_path = mount / "boot/initrd.img-6.18.0"
        initrd_size = initrd_path.stat().st_size
        initrd_sha = sha256(initrd_path)
        boot_cmd = _read(mount / "boot/boot.cmd")
        boot_scr = mount / "boot/boot.scr"
        busybox = mount / "bin/busybox"
        bb_stat = busybox.stat()
        bb_sha = sha256(busybox)
        image_sha = sha256(mount / "boot/Image")
        fdt_sha = selected_dtb_sha
        fstab_sha = hashlib.sha256(fstab.encode()).hexdigest()
        boot_scr_sha = sha256(boot_scr)
        boot_cmd_sha = hashlib.sha256(boot_cmd.encode()).hexdigest()
        modules_dep = mount / "lib/modules/6.18.0/modules.dep"
        modules_dep_sha = sha256(modules_dep) if modules_dep.exists() else ""
        trim_status = dpkg_trim.get("Status", "")
        trim_pkg_version = dpkg_trim.get("Version", "")
        masked = {}
        for unit in MASK_UNITS:
            masked[unit] = _systemctl_root(mount, unit)
        core = {}
        for unit in ("smbd.service", "nmbd.service", "trim_nginx.service", "trim_http_cgi.service", "docker.service", "containerd.service", "nfs-server.service"):
            core[unit] = (mount / "etc/systemd/system/multi-user.target.wants" / unit).is_symlink()
        # Load ranges from the actual final files and explicit X5 addresses.
        ranges = {
            "kernel": {"start": KERNEL_LOAD, "size": image_size, "end": KERNEL_LOAD + image_size},
            "fdt": {"start": FDT_LOAD, "size": fdt_size, "end": FDT_LOAD + fdt_size},
            "initrd": {"start": INITRD_LOAD, "size": initrd_size, "end": INITRD_LOAD + initrd_size},
        }
        no_overlap = (
            ranges["fdt"]["end"] <= ranges["kernel"]["start"]
            and ranges["kernel"]["end"] <= ranges["initrd"]["start"]
            and ranges["initrd"]["end"] <= DRAM_END
            and ranges["kernel"]["start"] >= DRAM_START
            and ranges["fdt"]["start"] >= DRAM_START
            and ranges["initrd"]["start"] >= DRAM_START
        )
        live_lmb_overlap = [
            name for name, r in ranges.items()
            if r["start"] < LIVE_UBOOT_LMB[1] and LIVE_UBOOT_LMB[0] < r["end"]
        ]
        reserved_overlap = []
        for name, r in ranges.items():
            for res in reservations:
                if r["start"] < res["end"] and res["start"] < r["end"]:
                    reserved_overlap.append({"load": name, "reservation": res["node"]})
        no_reserved_overlap = not reserved_overlap
        checks = {
            "final_image_sha256": final_sha,
            "final_image_size": image.stat().st_size,
            "final_p1_sha256": p1_sha,
            "final_p2_sha256": p2_sha,
            "p1_region_matches_component": expected_p1_sha256 is None or p1_sha == expected_p1_sha256,
            "p2_region_matches_component": expected_p2_sha256 is None or p2_sha == expected_p2_sha256,
            "mbr_table": parsed["table"],
            "partition_count": len(parsed["partitions"]),
            "p1_type": p1.get("mbr_type"),
            "p1_fs": fs1.get("type") == "vfat",
            "p2_type": p2.get("mbr_type"),
            "p2_fs": fs2.get("type") == "ext4",
            "p2_bootable": p2.get("mbr_type") == 0x83 and bool(p2.get("bootable")),
            "p2_uuid": fs_uuid,
            "fstab_has_uuid": f"UUID={fs_uuid}" in fstab,
            "fstab_sha256": fstab_sha,
            "os_id": osrel.get("ID"),
            "os_version_id": osrel.get("VERSION_ID"),
            "os_pretty_name": osrel.get("PRETTY_NAME"),
            "trim_version_file": trim_ver,
            "trim_package_version": trim_pkg_version,
            "trim_package_status": trim_status,
            "trim_package_matches": trim_ver == "1.2.0701" and trim_pkg_version == "1.2.0701" and trim_status == "install ok installed",
            "modules": modules,
            "modules_only_6180": modules == ["6.18.0"],
            "modules_ko_count": len(list((mount / "lib/modules/6.18.0").rglob("*.ko"))),
            "modules_dep_sha256": modules_dep_sha,
            "boot_files": boot_names,
            "boot_has_image": "Image" in boot_names,
            "boot_has_initrd": "initrd.img-6.18.0" in boot_names,
            "boot_has_scr": "boot.scr" in boot_names,
            "boot_has_dtb": "hobot" in boot_names,
            "dtb_count": len(dtbs),
            "dtb_has_x5_rdk": "x5-rdk.dtb" in dtbs,
            "initrd_size": initrd_size,
            "initrd_sha256": initrd_sha,
            "initrd_gt_512m": initrd_size > 512 * MIB,
            "initrd_gzip": run(["gzip", "-t", str(initrd_path)], timeout=120).returncode == 0,
            "initrd_mountpoints": True,
            "initrd_busybox": True,
            "initrd_btrfs": True,
            "boot_cmd_sha256": boot_cmd_sha,
            "boot_scr_sha256": boot_scr_sha,
            "boot_scr_image": "Linux Script" in run(["mkimage", "-l", str(boot_scr)], timeout=60).stdout,
            "boot_cmd_has_size_arg": '${ramdisk_addr_r}:${initrd_size}' in boot_cmd,
            "boot_cmd_fail_closed": all(x in boot_cmd for x in ("ERROR: cannot load", "ERROR: cannot load FDT", "ERROR: cannot load kernel", "exit")),
            "boot_cmd_explicit_addrs": all(x in boot_cmd for x in ("setenv kernel_addr_r 0x85800000", "setenv fdt_addr_r 0x84000000", "setenv ramdisk_addr_r 0xc0000000")),
            "boot_cmd_initrd_relocation": all(x in boot_cmd for x in ("setenv initrd_high 0xffffffff", "setenv bootm_low 0x84000000", "setenv bootm_size 0x100000000")),
            "boot_cmd_initrd_max": "0x20000000" in boot_cmd,
            "busybox_uid0": bb_stat.st_uid == 0 and bb_stat.st_gid == 0,
            "busybox_mode": oct(bb_stat.st_mode & 0o7777),
            "busybox_sha256": bb_sha,
            "busybox_arch": "",
            "busybox_aarch64": False,
            "image_sha256": image_sha,
            "fdt_sha256": fdt_sha,
            "image_arch": "",
            "image_arm64": False,
            "source_identity_kept": "nanopi-r3s" in _read(mount / "etc/fnos-x5/source-identity.txt"),
            "x5_identity": _read(mount / "etc/device_info/boot_board").strip(),
            "masked_units": masked,
            "all_units_masked": all("masked" in v.lower() for v in masked.values()),
            "core_services": core,
            "core_services_retained": all(core.values()),
            "load_ranges": ranges,
            "load_ranges_no_overlap": no_overlap,
            "load_ranges_within_dram": ranges["initrd"]["end"] <= DRAM_END,
            "selected_dtb": "x5-rdk-v1p0.dtb",
            "selected_dtb_size": fdt_size,
            "selected_dtb_sha256": selected_dtb_sha,
            "selected_dtb_matches": selected_dtb_sha == "5427dd2c2c149c71ada2b258a36f5158abd890e229517895ae930aa02b6a67eb",
            "reserved_memory": reservations,
            "reserved_memory_overlap": reserved_overlap,
            "reserved_memory_avoided": no_reserved_overlap,
            "live_uboot_lmb": {"start": hex(LIVE_UBOOT_LMB[0]), "end": hex(LIVE_UBOOT_LMB[1])},
            "live_uboot_lmb_overlap": live_lmb_overlap,
            "live_uboot_lmb_avoided": not live_lmb_overlap,
            "boot_cmd_no_colon": "then :" not in boot_cmd and "if :" not in boot_cmd,
            "boot_cmd_success_echo": "Loaded FDT" in boot_cmd and "Loaded kernel" in boot_cmd,
            "boot_cmd_board_0302": 'hb_board_id' in boot_cmd and '0x0302' in boot_cmd and 'x5-rdk-v1p0.dtb' in boot_cmd,
            "kernel_fdt_load_checked": "ERROR: cannot load FDT" in boot_cmd and "ERROR: cannot load kernel" in boot_cmd,
            "no_rk_artifacts": all("rk35" not in n.lower() and "rk3566" not in n.lower() and "rockchip" not in n.lower() for n in boot_names + dtbs),
        }
        # Architecture and initramfs content are checked against final files.
        proc = run(["readelf", "-h", str(busybox)], timeout=60)
        checks["busybox_arch"] = proc.stdout
        checks["busybox_aarch64"] = "AArch64" in proc.stdout
        proc = run(["file", str(mount / "boot/Image")], timeout=60)
        checks["image_arch"] = proc.stdout
        checks["image_arm64"] = "ARM64" in proc.stdout or "aarch64" in proc.stdout
        listing = run(["bash", "-c", "gzip -dc " + str(initrd_path) + " | cpio -it 2>/dev/null"], timeout=300).stdout
        lines = set(listing.splitlines())
        checks["initrd_mountpoints"] = all(d in lines for d in ("proc", "sys", "dev", "root", "tmp"))
        checks["initrd_busybox"] = "bin/busybox" in lines
        checks["initrd_btrfs"] = "lib/modules/6.18.0/kernel/fs/btrfs/btrfs.ko" in lines
        # Explicit masked-unit symlink check, independent of systemctl.
        checks["mask_symlinks"] = {u: (mount / "etc/systemd/system" / u).is_symlink() and os.readlink(mount / "etc/systemd/system" / u) == "/dev/null" for u in MASK_UNITS}
        checks["mask_symlinks_all"] = all(checks["mask_symlinks"].values())
        checks["masked_source_preserved"] = all((mount / "etc/fnos-x5/disabled-units" / u).is_file() for u in ("resize-rootfs.service", "updatemgr.service"))
        checks["old_negative_condition_absent"] = not any((mount / "etc/systemd/system" / u / "90-x5-guard.conf").exists() for u in ("resize-rootfs.service", "updatemgr.service"))
        checks["initrd_size_positive"] = 0 < initrd_size <= INITRD_MAX
        critical = [
            "p2_bootable", "p1_fs", "p2_fs", "fstab_has_uuid", "p1_region_matches_component", "p2_region_matches_component", "trim_package_matches", "modules_only_6180",
            "boot_has_image", "boot_has_initrd", "boot_has_scr", "boot_has_dtb", "dtb_has_x5_rdk",
            "initrd_gzip", "initrd_mountpoints", "initrd_busybox", "initrd_btrfs",
            "boot_scr_image", "boot_cmd_has_size_arg", "boot_cmd_fail_closed", "boot_cmd_no_colon", "boot_cmd_success_echo", "live_uboot_lmb_avoided",
            "boot_cmd_explicit_addrs", "boot_cmd_initrd_relocation", "boot_cmd_initrd_max", "initrd_size_positive", "masked_source_preserved", "old_negative_condition_absent", "busybox_uid0", "busybox_aarch64",
            "image_arm64", "source_identity_kept", "all_units_masked", "mask_symlinks_all",
            "core_services_retained", "load_ranges_no_overlap", "load_ranges_within_dram", "reserved_memory_avoided", "selected_dtb_matches", "boot_cmd_board_0302", "kernel_fdt_load_checked", "no_rk_artifacts",
        ]
        if expected_runtime is not None:
            checks["platform_runtime"] = verify_runtime(mount, expected_runtime, require_root_owner=True)
            checks["platform_runtime_verified"] = True
            critical.append("platform_runtime_verified")
        if expected_home_storage is not None:
            checks["home_storage"] = verify_home_storage(mount, expected_home_storage, require_root_owner=True)
            checks["home_storage_verified"] = True
            critical.append("home_storage_verified")
        if expected_native_media is not None:
            checks["native_media"] = verify_native_media(mount, expected_native_media, require_root_owner=True)
            checks["native_media_verified"] = True
            critical.append("native_media_verified")
        if expected_native_photos is not None:
            checks["native_photos"] = verify_native_photos(mount, expected_native_photos, require_root_owner=True)
            checks["native_photos_verified"] = True
            critical.append("native_photos_verified")
        if expected_product_inputs is not None:
            checks["product_inputs"] = verify_product_inputs(mount, expected_product_inputs, require_root_owner=True)
            checks["product_kernel_requirements"] = validate_product_kernel(mount / 'boot/config-6.18.0')
            checks["product_inputs_verified"] = True
            critical.append("product_inputs_verified")
        if expected_native_semantic768 is not None:
            checks["native_semantic768"] = verify_native_semantic768(mount, expected_native_semantic768, require_root_owner=True)
            checks["native_semantic768_verified"] = True
            critical.append("native_semantic768_verified")
        checks["critical"] = critical
        checks["all_pass"] = all(bool(checks[k]) for k in critical)
        checks["os"] = {"os_release": osrel, "trim_version_file": trim_ver, "trim_package": {"version": trim_pkg_version, "status": trim_status}}
        checks["boot"] = {"cmd_sha256": boot_cmd_sha, "scr_sha256": boot_scr_sha, "initrd_size": initrd_size, "initrd_sha256": initrd_sha, "selected_dtb": "x5-rdk-v1p0.dtb", "selected_dtb_sha256": selected_dtb_sha, "selected_dtb_size": fdt_size, "addresses": {"kernel": hex(KERNEL_LOAD), "fdt": hex(FDT_LOAD), "initrd": hex(INITRD_LOAD), "initrd_arg": f"0x{INITRD_LOAD:x}:0x{initrd_size:x}"}, "ranges": ranges, "reserved_memory": reservations, "live_uboot_lmb": {"start": hex(LIVE_UBOOT_LMB[0]), "end": hex(LIVE_UBOOT_LMB[1])}}
        checks["services"] = {"masked_units": masked, "mask_symlinks": checks["mask_symlinks"], "core_services": core}
        checks["final_hashes"] = {"boot_cmd": boot_cmd_sha, "boot_scr": boot_scr_sha, "initrd": initrd_sha, "image": image_sha, "fdt": fdt_sha, "busybox": bb_sha, "modules_dep": modules_dep_sha, "fstab": fstab_sha, "final_image": final_sha, "final_p1": p1_sha, "final_p2": p2_sha}
        return checks
    finally:
        if mounted:
            subprocess.run(["fusermount3", "-u", str(mount)], capture_output=True)
            time.sleep(0.5)
            shutil.rmtree(mount, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assemble X5 + fnOS candidate image (run under fakeroot)")
    ap.add_argument("--source-rootfs", type=Path, required=True, help="source btrfs partition image")
    ap.add_argument("--x5-artifacts", type=Path, required=True, help="dir with Image-6.18.0/dtbs/modules-root/config")
    ap.add_argument("--boot-cmd", type=Path, required=True, help="real X5 boot.cmd")
    ap.add_argument("--mbr", type=Path, required=True, help="X5 base 512-byte MBR")
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--rootfs-size", default="12G")
    ap.add_argument("--rootfs-uuid", default=None)
    ap.add_argument("--source-image-sha256", required=True)
    ap.add_argument("--release", default="r1")
    ap.add_argument("--runtime-sources", type=Path, help="pinned X5 runtime repositories; embed Wi-Fi firmware and codec/BPU libraries")
    ap.add_argument("--bpu-module", type=Path, help="exact reviewed legacy 6.18.0 BPU module; requires --runtime-sources")
    ap.add_argument("--home-acl-library", type=Path, help="embed household shared-data policy for native storage and application services")
    ap.add_argument("--home-ftp-library", type=Path, help="embed portable authenticated FTP data-volume view; requires --home-acl-library")
    ap.add_argument("--native-media-library", type=Path, help="embed reviewed X5 native mediasrv codec adapter; requires --runtime-sources")
    ap.add_argument("--native-media-decode", action="store_true", help="enable reviewed 8-bit H264/HEVC VPU decode; requires matching adapter build manifest")
    ap.add_argument("--native-photos-assets", type=Path, help="embed pinned FaceID128 worker, model and Python runtime; requires --runtime-sources and --bpu-module")
    ap.add_argument("--product-inputs", type=Path, help="clean native app installers/model caches and automatic native installation after storage initialization")
    ap.add_argument("--native-semantic-cpu", action="store_true", help="record verified native 768D CPU default; do not install dormant BPU semantic payload")
    ap.add_argument("--skip-restore", action="store_true")
    args = ap.parse_args(argv)
    if args.bpu_module and not args.runtime_sources:
        ap.error("--bpu-module requires --runtime-sources")
    if args.native_media_library and not args.runtime_sources:
        ap.error("--native-media-library requires --runtime-sources")
    if args.native_media_decode and not args.native_media_library:
        ap.error("--native-media-decode requires --native-media-library")
    if args.native_photos_assets and (not args.runtime_sources or not args.bpu_module):
        ap.error("--native-photos-assets requires --runtime-sources and --bpu-module")
    if args.home_ftp_library and not args.home_acl_library:
        ap.error("--home-ftp-library requires --home-acl-library")
    if args.product_inputs and not args.home_acl_library:
        ap.error("--product-inputs requires --home-acl-library for native application installation")
    if args.product_inputs and not args.native_photos_assets:
        ap.error("--product-inputs requires --native-photos-assets for the bundled AI Python runtime")
    if "FAKEROOTKEY" not in os.environ:
        print("error: must run under fakeroot to preserve uid/gid/xattrs", file=sys.stderr)
        return 2
    kernel_requirements = (validate_product_kernel(args.x5_artifacts / 'config-6.18.0')
                           if args.product_inputs else None)
    args.work.mkdir(parents=True, exist_ok=True)
    root = args.work / "rootfs"
    if not args.skip_restore:
        log("restoring fnOS btrfs rootfs with owner/mode/xattrs")
        restore_rootfs(args.source_rootfs, root)
    elif not (root / "etc/os-release").exists():
        print(f"error: --skip-restore but {root} is not a restored rootfs", file=sys.stderr)
        return 2
    fs_uuid = args.rootfs_uuid or str(uuid.uuid4())
    log(f"rootfs UUID: {fs_uuid}")
    prep = prepare_rootfs(root, args.x5_artifacts, args.boot_cmd, args.source_image_sha256, fs_uuid)
    if kernel_requirements:
        prep['product_kernel_requirements'] = kernel_requirements
    if args.runtime_sources:
        prep["platform_runtime"] = stage_runtime(root, args.runtime_sources, args.bpu_module)
        for path in ("vendor", "usr/hobot", "usr/lib/hbbpu", "usr/share/fnos-x5"):
            chown_root(root / path)
        for path in ("etc/ld.so.conf.d/fnos-x5-runtime.conf", "etc/modules-load.d/fnos-x5-wifi.conf",
                     "usr/lib/systemd/system/fnos-x5-runtime.service"):
            chown_root(root / path)
        run(["chown", "-h", "0:0", str(root / "etc/systemd/system/multi-user.target.wants/fnos-x5-runtime.service")])
        if args.bpu_module:
            chown_root(root / "usr/lib/modules/6.18.0/updates")
            chown_root(root / "etc/modules-load.d/fnos-x5-bpu.conf")
            run(["depmod", "-b", str(root), "6.18.0"], timeout=1200)
    if args.home_acl_library:
        prep["home_storage"] = stage_home_storage(root, args.home_acl_library, args.home_ftp_library,
                                                  enable_cloud_cache=bool(args.product_inputs))
        for path in ("usr/lib/fnos-x5", "usr/share/fnos-x5", "etc/fnos-x5"):
            chown_root(root / path)
        for unit in (*HOME_STORAGE_UNITS, "trim_trashbind"):
            chown_root(root / "etc/systemd/system" / (unit + ".service.d"))
    if args.native_media_library:
        prep["native_media"] = stage_native_media(root, args.native_media_library, enable_decode=args.native_media_decode)
        for path in ("usr/lib/fnos-x5", "usr/share/fnos-x5", "etc/fnos-x5",
                     "etc/systemd/system/mediasrv.service.d"):
            chown_root(root / path)
    if args.native_photos_assets:
        prep["native_photos"] = stage_native_photos(root, args.native_photos_assets)
        for path in ("usr/lib/fnos-x5", "usr/share/fnos-x5", "etc/fnos-x5",
                     "usr/trim/lib/ai_manager/com.trim.face_det/cp311/face_det.bin"):
            chown_root(root / path)
    if args.product_inputs:
        prep["product_inputs"] = stage_product_inputs(root, args.product_inputs)
        # Include every product drop-in and factory identity path, not only
        # the original timer. Preserve unrelated application data ownership.
        report = prep['product_inputs']
        paths = {root / 'usr/share/fnos-x5/product-bootstrap-manifest.json'}
        for row in [*report['files'], *report.get('factory_identity', {}).get('files', [])]:
            path = root / row['path']
            while path != root:
                paths.add(path)
                path = path.parent
        run(['chown', '-h', '0:0', *map(str, sorted(paths))])
    if args.native_semantic_cpu:
        prep['native_semantic768'] = stage_native_semantic768(root)
        chown_root(root / 'usr/share/fnos-x5/native-semantic768-manifest.json')
    initrd = build_initramfs(root, args.x5_artifacts, args.work)
    copy_file(initrd, root / "boot" / "initrd.img-6.18.0", 0o644)
    size_map = {"K": 1024, "M": MIB, "G": GIB, "T": 1024 * GIB}
    m = re.fullmatch(r"(\d+)([KMGT])", args.rootfs_size)
    if not m:
        print("error: --rootfs-size must look like 12G", file=sys.stderr)
        return 2
    size = int(m.group(1)) * size_map[m.group(2)]
    p2 = args.work / "rootfs.ext4"
    p2info = build_ext4(root, p2, size, fs_uuid)
    p1 = args.work / "config.vfat"
    p1info = build_fat(p1)
    outinfo = assemble_image(args.mbr, p1, p2, args.output)
    # A4: every critical check reads the final image/p2, not staging.
    checks = verify_final(args.output, args.work, p1info["sha256"], p2info["sha256"], prep.get("platform_runtime"), prep.get("home_storage"), prep.get("native_media"), prep.get("native_photos"), prep.get("product_inputs"), prep.get('native_semantic768'))
    manifest = {
        "schema": "fnos-x5-assembly/v2",
        "release": args.release,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "offline-private-image; fakeroot; final-p2 readback; no physical disk",
        "source": {
            "fnos_image_sha256": args.source_image_sha256,
            "rootfs_partition": str(args.source_rootfs),
            "x5_artifacts": str(args.x5_artifacts),
            "boot_cmd": str(args.boot_cmd),
            "mbr": str(args.mbr),
        },
        "candidate": {
            "release": args.release,
            "path": str(args.output),
            "sha256": checks["final_image_sha256"],
            "size": checks["final_image_size"],
        },
        "components": {
            "p1": p1info,
            "p2": {**p2info, "path": str(p2)},
            "initramfs_source": {"path": str(initrd), "size": initrd.stat().st_size, "sha256": sha256(initrd)},
            "kernel": {"path": str(args.x5_artifacts / "Image-6.18.0"), "sha256": sha256(args.x5_artifacts / "Image-6.18.0")},
            "fdt": {"path": str(args.x5_artifacts / "dtbs/x5-rdk.dtb"), "sha256": sha256(args.x5_artifacts / "dtbs/x5-rdk.dtb")},
        },
        "rootfs": {"uuid": fs_uuid, "size": p2info["size"], "fstype": "ext4"},
        "os": checks["os"],
        "boot": checks["boot"],
        "services": checks["services"],
        "changes": prep,
        "final_readback": checks,
        "verification": checks,
        "verification_policy": "all critical checks are read from the final .img; p1/p2 staging hashes are component evidence only",
        "open_p0": [
            "real board boot/network/SMB/upgrade not tested",
            ("X5 runtime embedded; native media/photos acceleration still requires integration"
             if args.runtime_sources else "X5 firmware files for optional wireless/GPU/BPU are not imported"),
            "rootfs free space is fixed; no first-boot resize",
            "SPI Miniboot/U-Boot dependency not exercised",
        ],
        "note": "The candidate is Debian 12 userspace with fnOS trim 1.2.0701, not a fake ID=fnos os-release. Source R3S identity is retained under /etc/fnos-x5.",
    }
    manifest_path = args.output.with_suffix(args.output.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"manifest: {manifest_path}")
    if not checks["all_pass"]:
        print("error: final readback verification failed", file=sys.stderr)
        return 3
    log(f"candidate: {args.output} sha256={checks['final_image_sha256']} size={checks['final_image_size']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
