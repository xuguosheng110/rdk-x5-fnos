"""Static, read-only dependency audit for an fnOS ARM image."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import posixpath
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Iterable

from .elf import ElfError, max_glibc_version, parse_elf
from .image import InputImage, ImageError, parse_partitions, prepare_image, probe_filesystem
from .rootfs import (
    BtrfsBackend,
    DirectoryBackend,
    PathRefused,
    Entry,
    Ext4Backend,
    RootFS,
    RootFSError,
    SquashfsBackend,
    ToolMissing,
    ToolRunner,
    TooLarge,
    validate_image_path,
)

SCHEMA = "fnos-input-audit/v1"
TOOL_VERSION = "0.1.0"

KEY_BINARIES = [
    "/usr/lib/systemd/systemd",
    "/lib/systemd/systemd",
    "/usr/bin/systemd",
    "/usr/sbin/smbd",
    "/usr/sbin/nmbd",
    "/usr/bin/smbd",
    "/bin/bash",
    "/usr/bin/bash",
    "/usr/sbin/sshd",
    "/usr/bin/ssh",
    "/usr/sbin/smartctl",
    "/usr/bin/dockerd",
    "/usr/bin/containerd",
    "/usr/trim/bin/trim",
    "/usr/trim/bin/updatemgr",
    "/usr/trim/bin/trim_init",
    "/usr/trim/bin/smbftpd",
    "/bin/busybox",
]

LOADER_PATHS = [
    "/lib/ld-linux-aarch64.so.1",
    "/lib64/ld-linux-aarch64.so.1",
    "/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1",
    "/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1",
    "/lib/ld-musl-aarch64.so.1",
]

LIBC_PATHS = [
    "/lib/aarch64-linux-gnu/libc.so.6",
    "/usr/lib/aarch64-linux-gnu/libc.so.6",
    "/lib/libc.so.6",
    "/usr/lib/libc.so.6",
    "/lib/ld-musl-aarch64.so.1",
]

KEY_PACKAGES = [
    "libc6",
    "systemd",
    "systemd-sysv",
    "udev",
    "samba",
    "samba-common",
    "samba-common-bin",
    "nfs-kernel-server",
    "btrfs-progs",
    "mdadm",
    "lvm2",
    "cryptsetup",
    "smartmontools",
    "parted",
    "gdisk",
    "e2fsprogs",
    "util-linux",
    "iproute2",
    "openssh-server",
    "docker.io",
    "containerd",
    "network-manager",
    "ifupdown",
    "netplan.io",
    "linux-image-generic",
    "initramfs-tools",
    "dracut",
]

CONFIG_CHECKS = [
    # symbol, needed_for, severity when missing, condition
    ("CONFIG_EXT4_FS", "ext4 root filesystem", "P0", "rootfs:ext4"),
    ("CONFIG_EXT4_FS_POSIX_ACL", "SMB/NFS ACL and permissions", "P0", "always"),
    ("CONFIG_EXT4_FS_SECURITY", "xattr/security labels on ext4", "P1", "always"),
    ("CONFIG_BTRFS_FS", "Btrfs data pool support", "P1", "btrfs"),
    ("CONFIG_XFS_FS", "XFS data pool support", "P2", "xfs"),
    ("CONFIG_VFAT_FS", "FAT boot partition", "P0", "boot:fat"),
    ("CONFIG_FAT_FS", "FAT support", "P0", "boot:fat"),
    ("CONFIG_MSDOS_FS", "FAT support", "P1", "boot:fat"),
    ("CONFIG_BLK_DEV_SD", "SCSI/SATA/USB block devices", "P0", "always"),
    ("CONFIG_SCSI", "SCSI core for UAS", "P0", "always"),
    ("CONFIG_USB_STORAGE", "USB mass storage", "P0", "always"),
    ("CONFIG_USB_UAS", "USB Attached SCSI (UAS)", "P1", "always"),
    ("CONFIG_FUSE_FS", "FUSE-based apps", "P2", "always"),
    ("CONFIG_OVERLAY_FS", "container overlayfs", "P1", "docker"),
    ("CONFIG_CGROUPS", "systemd/container cgroups", "P0", "always"),
    ("CONFIG_NAMESPACES", "container namespaces", "P1", "docker"),
    ("CONFIG_NET_NS", "container network namespace", "P1", "docker"),
    ("CONFIG_USER_NS", "rootless containers", "P2", "docker"),
    ("CONFIG_BRIDGE", "bridged networking", "P2", "always"),
    ("CONFIG_NETFILTER", "firewall/NAT", "P1", "always"),
    ("CONFIG_NF_CONNTRACK", "firewall state tracking", "P1", "always"),
    ("CONFIG_VLAN_8021Q", "VLAN", "P2", "always"),
    ("CONFIG_BONDING", "link aggregation", "P2", "always"),
    ("CONFIG_MD", "software RAID", "P1", "mdadm"),
    ("CONFIG_BLK_DEV_DM", "device-mapper/LVM", "P1", "lvm"),
    ("CONFIG_DM_CRYPT", "encrypted volumes", "P2", "cryptsetup"),
    ("CONFIG_CRYPTO_XTS", "AES-XTS disk encryption", "P2", "cryptsetup"),
    ("CONFIG_CIFS", "CIFS client", "P2", "always"),
    ("CONFIG_NFS_FS", "NFS client", "P2", "always"),
    ("CONFIG_NFSD", "NFS server", "P1", "nfs"),
    ("CONFIG_NFS_V4", "NFSv4 server", "P2", "nfs"),
    ("CONFIG_INOTIFY_USER", "file watching", "P0", "always"),
    ("CONFIG_FANOTIFY", "file access auditing", "P2", "always"),
    ("CONFIG_TMPFS_XATTR", "systemd tmpfiles/xattrs", "P1", "always"),
    ("CONFIG_SECURITY", "LSM framework", "P1", "always"),
    ("CONFIG_QUOTA", "user/group quotas", "P2", "always"),
    ("CONFIG_SQUASHFS", "squashfs images", "P2", "always"),
    ("CONFIG_ISO9660_FS", "ISO images", "P2", "always"),
    ("CONFIG_EFI_PARTITION", "GPT parsing", "P0", "always"),
    ("CONFIG_DEVTMPFS", "early /dev", "P0", "always"),
    ("CONFIG_PACKET", "DHCP/raw sockets", "P0", "always"),
    ("CONFIG_INET", "IPv4", "P0", "always"),
    ("CONFIG_X5_ETH", "X5 on-board Ethernet", "P0", "always"),
    ("CONFIG_MMC", "SD/eMMC", "P0", "always"),
    ("CONFIG_USB_XHCI_HCD", "USB 3.0 host", "P0", "always"),
    ("CONFIG_THERMAL", "thermal management", "P1", "always"),
    ("CONFIG_HWMON", "hardware monitoring", "P1", "always"),
    ("CONFIG_RTC_CLASS", "RTC", "P2", "always"),
    ("CONFIG_WATCHDOG", "watchdog", "P2", "always"),
    ("CONFIG_ZRAM", "zram swap (fnOS zramswap)", "P2", "always"),
    ("CONFIG_KVM", "virtualization (fnOS libvirtd)", "P2", "always"),
]

FLASH_KEYWORDS = [
    "spi",
    "nand",
    "mtd",
    "uboot",
    "u-boot",
    "boot.scr",
    "fw_setenv",
    "flash_erase",
    "mmcblk0boot",
    "idbloader",
    "rkdeveloptool",
]
BOOT_UPDATE_KEYWORDS = [
    "update-initramfs",
    "update-grub",
    "grub-install",
    "extlinux",
    "vmlinuz",
]
BOOT_KEYWORDS = FLASH_KEYWORDS + BOOT_UPDATE_KEYWORDS

RESIZE_KEYWORDS = ["resize2fs", "xfs_growfs", "btrfs filesystem resize", "growpart", "parted", "sfdisk", "sgdisk", "e2fsck"]

UPDATE_KEYWORDS = BOOT_KEYWORDS + [
    "apt-get upgrade",
    "apt upgrade",
    "dpkg -i",
    "fnos-update",
    "updatemgr",
    "liveupdate",
    "update-grub",
    "update-initramfs",
    "boot_family",
    "BootBoard",
]

TEXT_EXTENSIONS = {
    ".conf",
    ".cfg",
    ".ini",
    ".service",
    ".socket",
    ".target",
    ".timer",
    ".path",
    ".mount",
    ".automount",
    ".sh",
    ".init",
    ".rules",
    ".list",
    ".sources",
    ".network",
    ".link",
    ".netdev",
    ".yaml",
    ".yml",
    ".json",
    ".txt",
    ".cmd",
    ".env",
    ".fstab",
    ".hook",
}

TEXT_NAMES = {
    "fstab",
    "interfaces",
    "hostname",
    "hosts",
    "os-release",
    "debian_version",
    "resolv.conf",
    "exports",
    "smb.conf",
    "rc.local",
    "boot.cmd",
    "extlinux.conf",
    "grub.cfg",
    "uEnv.txt",
    "armbianEnv.txt",
    "inittab",
    "ld.so.conf",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _read_text(rootfs: RootFS, path: str, limit: int = 2 * 1024 * 1024) -> str | None:
    try:
        return rootfs.read_text(path, max_bytes=limit)
    except (RootFSError, PathRefused, PathRefusedError):
        return None


def _read_follow(rootfs: RootFS, path: str, limit: int) -> bytes | None:
    """Read a path, falling back to the symlink target for filesystems that do not follow links."""
    try:
        data = rootfs.read_bytes(path, max_bytes=limit)
    except (RootFSError, TooLarge):
        data = None
    if data:
        return data
    target = rootfs.readlink(path)
    if not target:
        return None
    resolved = target if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    try:
        return rootfs.read_bytes(resolved, max_bytes=limit)
    except (RootFSError, TooLarge):
        return None


def _parse_os_release(text: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not text:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        result[key] = value
    return result


def _parse_packages(text: str | None) -> dict[str, str]:
    packages: dict[str, str] = {}
    if not text:
        return packages
    current: dict[str, str] = {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current.get("Package") and "installed" in current.get("Status", ""):
                packages[current["Package"]] = current.get("Version", "")
            current = {}
            continue
        if line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key] = value.strip()
    return packages


def _parse_unit(text: str) -> dict:
    info = {"description": None, "exec_start": [], "exec_stop": [], "after": [], "wanted_by": [], "type": None}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if section == "Unit":
            if key == "Description":
                info["description"] = value
            elif key == "After":
                info["after"] = value.split()
        elif section == "Service":
            if key in ("ExecStart", "ExecStartPre", "ExecStartPost"):
                info["exec_start"].append(value)
            elif key in ("ExecStop", "ExecStopPost"):
                info["exec_stop"].append(value)
            elif key == "Type":
                info["type"] = value
        elif section == "Install" and key == "WantedBy":
            info["wanted_by"] = value.split()
    return info


def _keyword_pattern(keyword: str) -> re.Pattern:
    # Word-ish boundaries avoid false hits such as "spi" in "spindown" or
    # "nand" in "Nandinagari". Underscores are deliberately allowed so that
    # UPDATEMGR_BOOT_FAMILY still matches boot_family.
    return re.compile(r"(?i)(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])")


def _scan_text(path: str, text: str, keywords: Iterable[str], max_hits: int = 50) -> list[dict]:
    hits: list[dict] = []
    patterns = [(kw, _keyword_pattern(kw)) for kw in keywords]
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("#", ";")):
            continue
        for original, pattern in patterns:
            if pattern.search(line):
                hits.append({"path": path, "line": lineno, "keyword": original, "text": line.strip()[:240]})
                if len(hits) >= max_hits:
                    return hits
                break
    return hits


class PathRefusedError(ValueError):
    pass


def _sha256_directory(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(root).as_posix()
            h.update(rel.encode("utf-8", "replace"))
            h.update(b"\x00")
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(block)
    return h.hexdigest()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:120]


@dataclass
class Finding:
    id: str
    severity: str
    area: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    action: str = ""
    confidence: str = "observed"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "severity": self.severity,
            "area": self.area,
            "summary": self.summary,
            "evidence": self.evidence,
            "action": self.action,
            "confidence": self.confidence,
        }


class Auditor:
    def __init__(
        self,
        image: Path,
        work_dir: Path,
        out_dir: Path,
        kernel_config: Path | None = None,
        kernel_image: Path | None = None,
        dtb: Path | None = None,
        kernel_modules: Path | None = None,
        official_md5: str | None = None,
        source_url: str | None = None,
        rootfs_dir: Path | None = None,
    ) -> None:
        self.image_path = image
        self.work_dir = work_dir
        self.out_dir = out_dir
        self.kernel_config = kernel_config
        self.kernel_image = kernel_image
        self.dtb = dtb
        self.kernel_modules = kernel_modules
        self.official_md5 = official_md5.lower() if official_md5 else None
        self.source_url = source_url
        self.rootfs_dir = rootfs_dir
        self.runner = ToolRunner()
        self.findings: list[Finding] = []
        self.gaps: list[dict] = []
        self.text_files: list[dict] = []

    # ------------------------------------------------------------------
    def add_finding(self, fid, severity, area, summary, evidence=None, action="", confidence="observed"):
        self.findings.append(
            Finding(fid, severity, area, summary, evidence or [], action, confidence)
        )

    def add_gap(self, gid, area, summary, evidence=None):
        self.gaps.append({"id": gid, "area": area, "summary": summary, "evidence": evidence or []})

    # ------------------------------------------------------------------
    def collect_text(self, rootfs: RootFS) -> list[dict]:
        """Read bounded text files from known configuration trees only."""

        roots = [
            ("/etc", 5, 4000),
            ("/usr/lib/systemd/system", 2, 1200),
            ("/lib/systemd/system", 2, 1200),
            ("/usr/lib/fnos", 4, 800),
            ("/usr/share/fnos", 4, 800),
            ("/opt/fnos", 4, 400),
            ("/usr/local/fnos", 4, 400),
            ("/usr/trim", 6, 1500),
            ("/etc/init.d", 1, 200),
        ]
        seen: set[str] = set()
        collected: list[dict] = []
        for base, depth, cap in roots:
            if not rootfs.exists(base):
                continue
            count = 0
            for entry in rootfs.walk(base, max_depth=depth, max_entries=cap):
                if entry.is_dir or entry.is_symlink:
                    continue
                if entry.path in seen:
                    continue
                seen.add(entry.path)
                name = entry.name.lower()
                ext = os.path.splitext(name)[1]
                init_like = entry.path.startswith(("/etc/init.d/", "/etc/cron.", "/etc/rc")) or name in (
                    "rc.local",
                    "rc",
                )
                if name not in TEXT_NAMES and ext not in TEXT_EXTENSIONS and not init_like:
                    continue
                if entry.size > 512 * 1024:
                    continue
                try:
                    text = _read_text(rootfs, entry.path, 512 * 1024)
                except TooLarge:
                    continue
                if text is None:
                    continue
                collected.append(
                    {
                        "path": entry.path,
                        "size": entry.size,
                        "sha256": _sha256_bytes(text.encode("utf-8", "replace")),
                        "text": text,
                    }
                )
                count += 1
                if count >= cap:
                    break
        self.text_files = collected
        return collected

    # ------------------------------------------------------------------
    def analyze_packages(self, rootfs: RootFS) -> dict:
        status = _read_text(rootfs, "/var/lib/dpkg/status", 32 * 1024 * 1024)
        if status is None:
            self.add_gap("pkg-db-missing", "packages", "/var/lib/dpkg/status absent; package versions unknown")
        packages = _parse_packages(status)
        key = {name: packages.get(name) for name in KEY_PACKAGES if name in packages}
        for name, version in packages.items():
            if name == "trim":
                key.setdefault(name, version)
            elif name.startswith(("linux-image-", "linux-modules-", "linux-headers-")) and version and (
                "trim" in name or "6.18" in name
            ):
                key.setdefault(name, version)
        return {
            "manager": "dpkg" if packages else "unknown",
            "count": len(packages),
            "key": key,
            "installed": packages,
        }

    def analyze_elf(self, rootfs: RootFS) -> dict:
        samples: list[dict] = []
        foreign: list[dict] = []
        errors: list[dict] = []
        for path in KEY_BINARIES:
            if not rootfs.exists(path):
                continue
            entry = rootfs.stat(path)
            if not entry or entry.is_dir or entry.size > 64 * 1024 * 1024:
                continue
            try:
                data = rootfs.read_bytes(path, max_bytes=64 * 1024 * 1024)
            except (RootFSError, TooLarge) as exc:
                errors.append({"path": path, "error": str(exc)})
                continue
            if not data:
                continue
            try:
                info = parse_elf(data)
            except ElfError as exc:
                errors.append({"path": path, "error": str(exc)})
                continue
            record = {"path": path, "size": len(data), **info.as_dict()}
            samples.append(record)
            if not info.is_aarch64:
                foreign.append({"path": path, "machine": info.machine_name, "bits": info.bits})

        loader = None
        for path in LOADER_PATHS:
            if not rootfs.exists(path):
                continue
            target = rootfs.readlink(path)
            info = None
            data = _read_follow(rootfs, path, 8 * 1024 * 1024)
            if data:
                try:
                    info = parse_elf(data).as_dict()
                except ElfError as exc:
                    errors.append({"path": path, "error": str(exc)})
            loader = {"path": path, "symlink": target, "elf": info}
            break

        libc = None
        for path in LIBC_PATHS:
            if not rootfs.exists(path):
                continue
            target = rootfs.readlink(path)
            data = _read_follow(rootfs, path, 8 * 1024 * 1024)
            banner = None
            if data:
                m = re.search(rb"GNU C Library[^\x00]{0,160}", data)
                if m:
                    banner = m.group(0).decode("utf-8", "replace")
            libc = {"path": path, "symlink": target, "banner": banner}
            break

        versions: list[str] = []
        for sample in samples:
            versions.extend(sample.get("glibc_versions", []))
        return {
            "sample_count": len(samples),
            "samples": samples,
            "foreign": foreign,
            "errors": errors,
            "loader": loader,
            "libc": libc,
            "max_glibc_requirement": max_glibc_version(versions),
            "note": "sample of key binaries, not an exhaustive ELF census",
        }

    def analyze_systemd(self, rootfs: RootFS) -> dict:
        enabled: list[dict] = []
        for base in ("/etc/systemd/system",):
            if not rootfs.exists(base):
                continue
            for top in rootfs.listdir(base):
                if top.is_dir and (top.name.endswith(".wants") or top.name.endswith(".requires")):
                    for unit in rootfs.listdir(top.path):
                        target = rootfs.readlink(unit.path)
                        enabled.append({"unit": unit.name, "link": target, "wants": top.name})
                elif top.name.endswith((".service", ".timer", ".socket", ".mount", ".path")):
                    target = rootfs.readlink(top.path)
                    if target:
                        enabled.append({"unit": top.name, "link": target, "wants": "(masked/alias)"})
        custom = []
        for base in ("/etc/systemd/system",):
            if not rootfs.exists(base):
                continue
            for entry in rootfs.listdir(base):
                if not entry.is_dir and entry.name.endswith((".service", ".timer", ".mount", ".socket", ".path")):
                    text = _read_text(rootfs, entry.path, 256 * 1024)
                    if text:
                        custom.append({"path": entry.path, "sha256": _sha256_bytes(text.encode()), **_parse_unit(text)})
        unit_index = {}
        for base in ("/usr/lib/systemd/system", "/lib/systemd/system"):
            if not rootfs.exists(base):
                continue
            for entry in rootfs.listdir(base):
                if entry.name.endswith((".service", ".timer", ".mount", ".socket")):
                    unit_index.setdefault(entry.name, entry.path)
        return {
            "enabled": sorted(enabled, key=lambda x: x["unit"]),
            "enabled_count": len(enabled),
            "custom_units": custom,
            "unit_files": unit_index,
            "init": self._init_info(rootfs),
        }

    def _init_info(self, rootfs: RootFS) -> dict:
        paths = ["/sbin/init", "/lib/systemd/systemd", "/usr/lib/systemd/systemd", "/etc/inittab"]
        info: dict = {"links": {}}
        for path in paths:
            if rootfs.exists(path):
                info["links"][path] = rootfs.readlink(path)
        info["systemd_binary"] = any(
            rootfs.exists(p) for p in ("/usr/lib/systemd/systemd", "/lib/systemd/systemd")
        )
        init_target = info["links"].get("/sbin/init")
        info["init_target"] = init_target
        info["is_systemd"] = info["systemd_binary"] or (init_target or "").endswith("systemd")
        return info

    def analyze_boot(self, rootfs: RootFS, partitions: dict) -> dict:
        boot_files = []
        for base in ("/boot", "/boot/efi"):
            if not rootfs.exists(base):
                continue
            for entry in rootfs.walk(base, max_depth=2, max_entries=400):
                if entry.is_dir:
                    continue
                boot_files.append(
                    {
                        "path": entry.path,
                        "size": entry.size,
                        "symlink": rootfs.readlink(entry.path),
                    }
                )
        configs = {}
        for path in (
            "/boot/extlinux/extlinux.conf",
            "/boot/grub/grub.cfg",
            "/boot/boot.cmd",
            "/boot/boot.scr",
            "/boot/uEnv.txt",
            "/boot/armbianEnv.txt",
            "/etc/fstab",
            "/etc/default/grub",
        ):
            text = _read_text(rootfs, path, 256 * 1024)
            if text is not None:
                configs[path] = {"sha256": _sha256_bytes(text.encode()), "text": text[:8000]}
        initramfs = []
        for entry in rootfs.listdir("/boot") if rootfs.exists("/boot") else []:
            if "initr" in entry.name.lower() or entry.name.startswith("initramfs"):
                initramfs.append({"path": entry.path, "size": entry.size, "symlink": rootfs.readlink(entry.path)})
        return {
            "files": boot_files,
            "configs": configs,
            "initramfs": initramfs,
            "partition_table": partitions.get("table"),
            "boot_partition": next(
                (p for p in partitions.get("partitions", []) if p.get("type") == "efi-system" or p.get("mbr_type") in (0x0B, 0x0C, 0xEF)),
                None,
            ),
        }

    def analyze_scripts(self, rootfs: RootFS, text_files: list[dict]) -> dict:
        resize_scripts = []
        update_refs: list[dict] = []
        boot_refs: list[dict] = []
        for item in text_files:
            path = item["path"]
            text = item["text"]
            low = path.lower()
            if any(k in low for k in ("resize", "expand", "grow", "firstboot")):
                if "systemd-firstboot" in low or "mdadm-grow" in low:
                    continue
                hits = _scan_text(path, text, RESIZE_KEYWORDS)
                service_ref = "ExecStart" in text or "ExecStartPre" in text
                if hits or service_ref or any(k in text for k in RESIZE_KEYWORDS):
                    resize_scripts.append(
                        {
                            "path": path,
                            "sha256": item["sha256"],
                            "matches": hits,
                            "service_ref": service_ref,
                        }
                    )
            hits = _scan_text(path, text, BOOT_KEYWORDS, max_hits=20)
            if hits:
                boot_refs.extend(hits)
            hits = _scan_text(path, text, UPDATE_KEYWORDS, max_hits=20)
            if hits:
                update_refs.extend(hits)
        # de-duplicate references by path/line/keyword
        def dedupe(items):
            seen = set()
            out = []
            for it in items:
                key = (it["path"], it["line"], it["keyword"])
                if key not in seen:
                    seen.add(key)
                    out.append(it)
            return out

        boot_refs = dedupe(boot_refs)
        update_refs = dedupe(update_refs)
        def is_flash_action(ref: dict) -> bool:
            keyword = ref["keyword"]
            if keyword in BOOT_UPDATE_KEYWORDS:
                return False
            if keyword in {"boot.scr", "fw_setenv", "flash_erase", "mmcblk0boot", "idbloader", "rkdeveloptool"}:
                return True
            # A bare "mtd"/"nand" in a case label or LED script is not a flash
            # operation; require an explicit action word on the same line.
            if keyword in {"spi", "nand", "mtd", "uboot", "u-boot"}:
                return re.search(r"(?i)\b(write|erase|flash|program|dd)\b", ref["text"]) is not None
            return False

        flash_refs = [r for r in boot_refs if is_flash_action(r)]
        if flash_refs:
            assessment = "flash_or_bootloader_references_found"
        elif boot_refs:
            assessment = "boot_update_references_found"
        elif update_refs:
            assessment = "update_mechanism_found_no_boot_reference"
        else:
            assessment = "no_update_path_found_in_scanned_text"
        return {
            "resize_scripts": resize_scripts,
            "boot_references": boot_refs,
            "flash_references": flash_refs,
            "update_references": update_refs,
            "assessment": assessment,
            "coverage": "heuristic scan of bounded /etc, systemd unit and fnOS vendor text files; not execution",
        }

    def analyze_update_binaries(self, rootfs: RootFS) -> dict:
        """Scan printable strings in updater binaries; never execute them."""
        targets = [
            "/usr/trim/bin/updatemgr",
            "/usr/trim/bin/liveupdate",
            "/usr/trim/bin/trim_init",
        ]
        keywords = [
            "spi", "nand", "mtd", "uboot", "u-boot", "boot.scr", "extlinux", "grub",
            "vmlinuz", "initrd", "flash", "boot_family", "boot_board", "BootBoard", "UPDATEMGR_BOOT_FAMILY",
        ]
        found = []
        for path in targets:
            entry = rootfs.stat(path)
            if not entry or entry.is_dir:
                continue
            if entry.size > 128 * 1024 * 1024:
                self.add_gap(f"update-binary-{path}", "update", f"{path} too large for string scan ({entry.size} bytes)")
                continue
            try:
                data = rootfs.read_bytes(path, max_bytes=128 * 1024 * 1024)
            except (RootFSError, TooLarge) as exc:
                self.add_gap(f"update-binary-{path}", "update", f"cannot read {path}: {exc}")
                continue
            if not data:
                continue
            strings = re.findall(rb"[ -~]{5,}", data)
            matches = []
            seen = set()
            patterns = [(k, _keyword_pattern(k)) for k in keywords]
            for raw in strings:
                text = raw.decode("ascii", "replace")
                for original, pattern in patterns:
                    if pattern.search(text) and (original, text[:120]) not in seen:
                        seen.add((original, text[:120]))
                        matches.append({"keyword": original, "text": text[:200]})
                        break
                if len(matches) >= 40:
                    break
            found.append(
                {
                    "path": path,
                    "size": entry.size,
                    "sha256": _sha256_bytes(data),
                    "matches": matches,
                }
            )
        flash_kw = {"spi", "nand", "mtd", "uboot", "u-boot", "boot.scr", "flash"}
        boot_kw = {"extlinux", "grub", "vmlinuz", "initrd", "boot_family", "boot_board", "BootBoard", "UPDATEMGR_BOOT_FAMILY"}
        flat = [m["keyword"] for b in found for m in b["matches"]]
        if any(k in flash_kw for k in flat):
            assessment = "binary_may_touch_boot_or_flash"
        elif any(k in boot_kw for k in flat):
            assessment = "binary_boot_aware_no_flash_strings"
        else:
            assessment = "no_boot_strings_in_sampled_update_binaries"
        return {"binaries": found, "assessment": assessment}

    def analyze_modules(self, rootfs: RootFS) -> dict:
        base = "/lib/modules"
        versions = []
        if rootfs.exists(base):
            for entry in rootfs.listdir(base):
                if entry.is_dir:
                    versions.append(entry.name)
        chosen = versions[0] if versions else None
        if not chosen:
            self.add_gap("modules-missing", "kernel-modules", "no /lib/modules/<release> found in rootfs")
            return {"versions": [], "kernelrelease": None, "count": 0, "notable": [], "builtin": [], "vermagic": []}
        root = f"/lib/modules/{chosen}"
        modules = []
        sizes: dict[str, int] = {}
        for entry in rootfs.walk(root, max_depth=8, max_entries=6000):
            if entry.is_dir:
                continue
            name = entry.name
            if name.endswith((".ko", ".ko.xz", ".ko.zst")):
                modules.append(entry.path)
                sizes[entry.path] = entry.size
        builtin_text = _read_text(rootfs, f"{root}/modules.builtin", 8 * 1024 * 1024) or ""
        builtin = [line.strip() for line in builtin_text.splitlines() if line.strip()]
        dep_text = _read_text(rootfs, f"{root}/modules.dep", 16 * 1024 * 1024) or ""
        dep_count = len([line for line in dep_text.splitlines() if line.strip()])
        notable_re = re.compile(
            r"(ext4|btrfs|xfs|overlay|bridge|nf_|nft|usb-storage|uas|md_mod|dm_|cifs|nfs|crypt|vfat|nls_|bonding|8021q|xhci|usbcore|scsi|gmac|stmmac|r8169|rtl8152|ax88179)"
        )
        notable = sorted({os.path.basename(m).split(".ko")[0] for m in modules if notable_re.search(os.path.basename(m))})
        vermagic = []
        for path in sorted(modules, key=lambda p: sizes.get(p, 0))[:20]:
            if sizes.get(path, 0) > 3 * 1024 * 1024:
                continue
            data = _read_follow(rootfs, path, 3 * 1024 * 1024)
            if not data:
                continue
            if len(vermagic) >= 10:
                break
            m = re.search(rb"vermagic=([^\x00\n]+)", data)
            if m:
                vermagic.append({"path": path, "vermagic": m.group(1).decode("utf-8", "replace")})
        return {
            "versions": versions,
            "kernelrelease": chosen,
            "count": len(modules),
            "modules_dep_lines": dep_count,
            "builtin_count": len(builtin),
            "notable": notable,
            "vermagic": vermagic,
            "paths": modules,
            "note": "module names/paths from rootfs; vermagic sampled from up to 10 small modules",
        }

    def analyze_network(self, rootfs: RootFS, modules: dict) -> dict:
        files = {}
        for path in (
            "/etc/network/interfaces",
            "/etc/hostname",
            "/etc/hosts",
            "/etc/resolv.conf",
        ):
            text = _read_text(rootfs, path, 256 * 1024)
            if text is not None:
                files[path] = text
        manager = []
        if rootfs.exists("/etc/NetworkManager"):
            manager.append("NetworkManager")
        if rootfs.exists("/etc/systemd/network"):
            manager.append("systemd-networkd")
        if "/etc/network/interfaces" in files:
            manager.append("ifupdown")
        if rootfs.exists("/etc/netplan"):
            manager.append("netplan")
        network_files = []
        for base in ("/etc/systemd/network", "/etc/NetworkManager/system-connections", "/etc/netplan"):
            if rootfs.exists(base):
                for entry in rootfs.walk(base, max_depth=2, max_entries=200):
                    if not entry.is_dir:
                        text = _read_text(rootfs, entry.path, 128 * 1024)
                        if text:
                            network_files.append({"path": entry.path, "text": text[:4000]})
        board = []
        for path in (
            "/etc/armbian-release",
            "/etc/board-release",
            "/etc/fnos-release",
            "/etc/fnos/board",
            "/etc/fnos/board.conf",
        ):
            text = _read_text(rootfs, path, 64 * 1024)
            if text:
                board.append({"path": path, "text": text[:2000]})
        if rootfs.exists("/etc/device_info"):
            for entry in rootfs.listdir("/etc/device_info"):
                if entry.is_dir or entry.size > 64 * 1024:
                    continue
                text = _read_text(rootfs, entry.path, 64 * 1024)
                if text:
                    board.append({"path": entry.path, "text": text.strip()[:2000]})
        nic_drivers = [
            m for m in modules.get("notable", [])
            if re.search(r"gmac|stmmac|r8169|rtl8152|ax88179|usbnet|cdc_ether|aic8800|8852|8723", m)
        ]
        return {
            "managers": manager,
            "files": files,
            "network_files": network_files,
            "board_identity": board,
            "nic_modules": nic_drivers,
            "note": "manager/interface detection is static; no link or DHCP test was performed",
        }

    def analyze_storage(self, rootfs: RootFS, packages: dict, text_files: list[dict]) -> dict:
        installed = packages.get("installed", {})
        binaries = {}
        for path in ("/usr/sbin/smbd", "/usr/sbin/nmbd", "/usr/sbin/exportfs", "/sbin/mdadm", "/sbin/lvm", "/sbin/btrfs", "/usr/sbin/cryptsetup"):
            binaries[path] = rootfs.exists(path)
        fstab_text = _read_text(rootfs, "/etc/fstab", 256 * 1024) or ""
        fstab = []
        for line in fstab_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) >= 3:
                fstab.append({"device": fields[0], "mount": fields[1], "type": fields[2], "options": fields[3] if len(fields) > 3 else ""})
        smb = _read_text(rootfs, "/etc/samba/smb.conf", 256 * 1024)
        shares = []
        vfs_objects: list[str] = []
        if smb:
            section = None
            for line in smb.splitlines():
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    if section.lower() != "global":
                        shares.append(section)
                elif line.lower().startswith("vfs objects"):
                    _, value = line.split("=", 1)
                    vfs_objects.extend(v for v in value.split() if v)
        vfs_found = {}
        for module in vfs_objects:
            candidates = [
                f"/usr/lib/aarch64-linux-gnu/samba/vfs/{module}.so",
                f"/usr/lib/samba/vfs/{module}.so",
            ]
            vfs_found[module] = next((p for p in candidates if rootfs.exists(p)), None)
        mounts = []
        if rootfs.exists("/etc/systemd/system"):
            for entry in rootfs.listdir("/etc/systemd/system"):
                if entry.name.endswith((".mount", ".automount")):
                    mounts.append(entry.name)
        udev = []
        if rootfs.exists("/etc/udev/rules.d"):
            udev = [e.name for e in rootfs.listdir("/etc/udev/rules.d") if not e.is_dir]
        pkgs = {
            name: installed.get(name)
            for name in (
                "samba",
                "samba-common-bin",
                "nfs-kernel-server",
                "btrfs-progs",
                "mdadm",
                "lvm2",
                "cryptsetup",
                "smartmontools",
                "parted",
                "gdisk",
                "e2fsprogs",
                "usbutils",
            )
            if name in installed
        }
        return {
            "samba": {
                "installed": any(k in installed for k in ("samba", "samba-common-bin")),
                "binaries": binaries,
                "config_present": smb is not None,
                "shares": shares,
                "vfs_objects": vfs_objects,
                "vfs_found": vfs_found,
                "vfs_missing": [m for m, p in vfs_found.items() if not p],
            },
            "nfs": {
                "installed": "nfs-kernel-server" in installed,
                "exports_present": rootfs.exists("/etc/exports"),
            },
            "packages": pkgs,
            "fstab": fstab,
            "mount_units": mounts,
            "udev_rules": udev,
            "storage_stack": {
                "mdadm": "mdadm" in installed or binaries.get("/sbin/mdadm"),
                "lvm": "lvm2" in installed or binaries.get("/sbin/lvm"),
                "btrfs": "btrfs-progs" in installed or binaries.get("/sbin/btrfs"),
                "cryptsetup": "cryptsetup" in installed or binaries.get("/usr/sbin/cryptsetup"),
                "smart": "smartmontools" in installed,
            },
        }

    # ------------------------------------------------------------------
    def kernel_check(
        self,
        rootfs_facts: dict,
        packages: dict,
        modules: dict,
        storage: dict,
        fnos_config_text: str | None = None,
    ) -> dict:
        if not self.kernel_config:
            self.add_gap("kernel-config-missing", "kernel", "no --kernel-config supplied; 6.18 config comparison not performed")
            return {"performed": False}
        text = self.kernel_config.read_text(encoding="utf-8", errors="replace")
        config = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("CONFIG_") and "=" in line:
                key, value = line.split("=", 1)
                config[key] = value
        fnos_config = {}
        for line in (fnos_config_text or "").splitlines():
            line = line.strip()
            if line.startswith("CONFIG_") and "=" in line:
                key, value = line.split("=", 1)
                fnos_config[key] = value
        installed = packages.get("installed", {})
        conditions = {
            "rootfs:ext4": rootfs_facts.get("fstype") in ("ext4", "ext2/3"),
            "btrfs": rootfs_facts.get("fstype") == "btrfs" or "btrfs-progs" in installed,
            "xfs": rootfs_facts.get("fstype") == "xfs",
            "boot:fat": True,
            "docker": "docker.io" in installed or "containerd" in installed,
            "mdadm": "mdadm" in installed,
            "lvm": "lvm2" in installed,
            "cryptsetup": "cryptsetup" in installed,
            "nfs": "nfs-kernel-server" in installed,
            "always": True,
        }
        checks = []
        for symbol, needed_for, severity, condition in CONFIG_CHECKS:
            if condition != "always" and not conditions.get(condition, False):
                continue
            value = config.get(symbol, "not set")
            if value == "y":
                status = "builtin"
            elif value == "m":
                status = "module"
            else:
                status = "missing"
            fnos_value = fnos_config.get(symbol)
            checks.append(
                {
                    "symbol": symbol,
                    "value": value,
                    "fnos_value": fnos_value,
                    "status": status,
                    "needed_for": needed_for,
                    "severity_if_missing": severity,
                }
            )
        for check in checks:
            if check["status"] == "missing" and check.get("fnos_value") not in ("y", "m"):
                self.add_finding(
                    f"kconfig-{check['symbol']}",
                    check["severity_if_missing"],
                    "kernel-config",
                    f"{check['symbol']} missing for {check['needed_for']}",
                    [f"6.18 config value: {check['value']}"],
                    "enable and rebuild the same 6.18 tree; re-run this audit",
                )
        fnos_delta = []
        for check in checks:
            fv = check.get("fnos_value")
            if fv in ("y", "m") and check["status"] == "missing":
                fnos_delta.append(check)
                self.add_finding(
                    f"fnos-kconfig-{check['symbol']}",
                    check["severity_if_missing"],
                    "kernel-config",
                    f"fnOS kernel enables {check['symbol']} but X5 6.18 lacks it ({check['needed_for']})",
                    [f"fnOS config: {fv}", f"X5 config: {check['value']}"],
                    "enable in the X5 6.18 config and rebuild; verify the matching service on hardware",
                    "observed",
                )
        return {
            "performed": True,
            "config_path": str(self.kernel_config),
            "config_sha256": _sha256_file(self.kernel_config),
            "fnos_config_sha256": _sha256_bytes(fnos_config_text.encode()) if fnos_config_text else None,
            "checks": checks,
            "missing": [c for c in checks if c["status"] == "missing"],
            "fnos_delta": fnos_delta,
        }

    def module_compare(self, modules: dict) -> dict:
        if not self.kernel_modules:
            self.add_gap("kernel-modules-missing", "kernel-modules", "no --kernel-modules supplied; module inventory comparison not performed")
            return {"performed": False}
        base = self.kernel_modules / "lib" / "modules"
        if not base.is_dir():
            base = self.kernel_modules
        x5 = set()
        x5_paths = []
        for path in base.rglob("*.ko"):
            x5.add(path.name.split(".ko")[0])
            x5_paths.append(path)
        x5_vermagic = set()
        for path in sorted(x5_paths, key=lambda p: p.stat().st_size)[:30]:
            try:
                data = path.read_bytes()[: 3 * 1024 * 1024]
            except OSError:
                continue
            m = re.search(rb"vermagic=([^\x00\n]+)", data)
            if m:
                x5_vermagic.add(m.group(1).decode("utf-8", "replace"))
        return {
            "performed": True,
            "x5_count": len(x5),
            "x5_modules": sorted(x5),
            "x5_vermagic": sorted(x5_vermagic),
            "fnos_modules": [],
        }

    # ------------------------------------------------------------------
    def build_verdict(self, facts: dict) -> dict:
        return {
            "static_audit": "complete" if facts.get("rootfs", {}).get("fstype") else "partial",
            "fnos_bootable_on_x5": "not_tested",
            "statement": (
                "Read-only static audit only. No image script, service or installer was executed, "
                "no mount or disk write was performed, and no X5 boot was attempted. "
                "This does not demonstrate that fnOS is usable on RDK X5."
            ),
        }

    def build_assembly(self, facts: dict) -> dict:
        kernel = {}
        if self.kernel_image:
            kernel["image"] = {"path": str(self.kernel_image), "size": self.kernel_image.stat().st_size, "sha256": _sha256_file(self.kernel_image)}
        if self.dtb:
            kernel["dtb"] = {"path": str(self.dtb), "size": self.dtb.stat().st_size, "sha256": _sha256_file(self.dtb)}
        if self.kernel_modules:
            kernel["modules"] = {"path": str(self.kernel_modules), "ko_count": len(list(self.kernel_modules.rglob("*.ko")))}
        if self.kernel_config:
            kernel["config"] = {"path": str(self.kernel_config), "sha256": _sha256_file(self.kernel_config)}
        return {
            "principle": (
                "Keep the RDK X5 Miniboot/U-Boot and the same-build 6.18 Image/DTB/modules; "
                "replace only the userspace with the audited fnOS ARM tree. Never copy RK idbloader/TF-A/U-Boot, "
                "Rockchip offsets, or spoof a Rockchip board identity."
            ),
            "kernel": kernel,
            "boot": {
                "input_boot_region_bytes": facts.get("image", {}).get("pre_partition_bytes", 0),
                "do_not_copy": [
                    n for n in facts.get("image", {}).get("partition_names", [])
                    if n and any(k in n.lower() for k in ("boot", "uboot", "idbloader", "trust"))
                ],
                "preserve": ["X5 Miniboot", "X5 U-Boot", "X5 boot.cmd/board-ID DTB selection logic"],
                "replace": ["fnOS boot.scr/extlinux/grub", "fnOS kernel Image", "fnOS DTB", "fnOS /lib/modules"],
                "root_argument": (
                    f"root=UUID={facts.get('uuid')}" if facts.get("uuid") else "root=UUID=<unresolved>"
                ),
                "root_argument_partuuid": (
                    f"root=PARTUUID={facts.get('unique_guid')}" if facts.get("unique_guid") else None
                ),
                "root_argument_note": (
                    "GPT PARTUUID uses the partition entry's unique partition GUID; "
                    "the source value is audit-only and must be recomputed after the final X5 GPT is assembled"
                ),
                "initramfs": "regenerate with X5 6.18 modules; do not reuse fnOS initramfs",
                "forbidden": ["Rockchip idbloader/TF-A/U-Boot", "Rockchip raw offsets", "RK DTB/model strings"],
            },
            "userspace": {
                "init": facts.get("systemd", {}).get("init"),
                "loader": facts.get("elf", {}).get("loader"),
                "libc": facts.get("elf", {}).get("libc"),
                "systemd_enabled_count": facts.get("systemd", {}).get("enabled_count"),
                "samba": facts.get("storage", {}).get("samba"),
            },
            "update_boundary": {
                "rule": "fnOS update must not write X5 boot/SPI/NAND; boot and kernel updates stay in the X5 BSP pipeline.",
                "observed": facts.get("scripts", {}).get("assessment"),
            },
            "p0_dependencies": [
                {
                    "id": "x5-boot-chain",
                    "summary": "Replace fnOS boot.cmd/boot.scr/fnEnv/grub/EFI and RK DTB with the X5 Miniboot/U-Boot + boot.cmd + x5-rdk.dtb path",
                    "evidence": f"input boot partition {facts.get('boot_partition', {}).get('partition_index')}; pre-partition raw region {facts.get('image', {}).get('pre_partition_bytes')} bytes",
                },
                {
                    "id": "kernel-module-abi",
                    "summary": "Rebuild all required modules against the X5 6.18.0 tree; do not load fnOS modules built for its own kernel",
                    "evidence": f"fnOS modules: {facts.get('modules', {}).get('kernelrelease')}; X5: 6.18.0",
                },
                {
                    "id": "rootfs-mount",
                    "summary": f"Mount the {facts.get('fstype')} rootfs by UUID; include the filesystem driver in the X5 initramfs",
                    "evidence": f"root UUID {facts.get('uuid')}; X5 CONFIG_BTRFS_FS=m",
                },
                {
                    "id": "wired-network",
                    "summary": "Bring up X5 Ethernet with X5_ETH/STMMAC, the X5 DTB PHY description and the fnOS NetworkManager configuration",
                    "evidence": f"fnOS managers: {facts.get('network', {}).get('managers')}",
                },
                {
                    "id": "smb-stack",
                    "summary": "Ship Samba with the exact VFS modules referenced by smb.conf and pass an SMB round-trip on a separate data disk",
                    "evidence": f"VFS: {facts.get('storage', {}).get('samba', {}).get('vfs_objects')}; missing: {facts.get('storage', {}).get('samba', {}).get('vfs_missing')}",
                },
                {
                    "id": "storage-kernel",
                    "summary": "Verify ext4/btrfs, ACL/xattr, USB/UAS/SCSI and md/dm in the X5 6.18 build and on hardware",
                    "evidence": "config audit checks in kernel_compare.config",
                },
                {
                    "id": "update-boundary",
                    "summary": "Block fnOS updatemgr from changing the X5 boot family/SPI/NAND; kernel and boot updates stay in the X5 BSP pipeline",
                    "evidence": f"updater: {facts.get('scripts', {}).get('update_binaries', {}).get('assessment')}",
                },
            ],
            "p1_before_delivery": [
                {
                    "id": "board-identity",
                    "summary": "Add an explicit X5 board identity path; never present a Rockchip boot_brand/boot_family",
                    "evidence": f"fnOS device_info: {facts.get('network', {}).get('board_identity')}",
                },
                {
                    "id": "first-boot-resize",
                    "summary": "Adapt fnOS resize-rootfs to the X5 partition layout and test on a clone",
                    "evidence": f"resize entries: {len(facts.get('scripts', {}).get('resize_scripts', []))}",
                },
                {
                    "id": "nfs-zram",
                    "summary": "Decide whether NFS server and zram swap are in P0/P1; X5 6.18 currently lacks CONFIG_ZRAM",
                    "evidence": "fnOS enables nfsd and zram",
                },
            ],
            "p2_deferred": [
                {"id": "bpu", "summary": "BPU/BPU runtime remains a separate closed-source path (6.18 bpu_hw_io input missing)"},
                {"id": "vpu-gpu", "summary": "VPU/GPU transcoding and fnOS media applications need their own ABI and permission validation"},
                {"id": "virtualization", "summary": "KVM/libvirt: X5 6.18 has no CONFIG_KVM; defer unless fnOS VM is in scope"},
                {"id": "wifi", "summary": "Wi-Fi/Bluetooth and out-of-tree vendor drivers are not P0 for the wired NAS baseline"},
                {"id": "raid-lvm-crypt", "summary": "RAID/LVM/encryption and SMART passthrough are P2 until the basic USB data disk works"},
            ],
            "hardware_confirmations": [
                "X5 U-Boot loads the same-build X5 Image and x5-rdk.dtb, then mounts the fnOS rootfs by UUID",
                "systemd reaches multi-user with the fnOS unit set and the X5 kernel",
                "Wired DHCP/static network, link state and sustained throughput",
                "USB 3.0 data disk enumeration, UAS/SCSI errors, mount by UUID and SMB round-trip",
                "First-boot resize on a clone, reboot persistence and free-space growth",
                "Update/rollback attempt with a before/after diff of boot/SPI/NAND",
                "BPU/VPU/GPU only after the P0 NAS baseline; record taint and firmware",
            ],
            "gates": [
                "P0: kernel config symbols and modules required by the observed rootfs/services are present in the same 6.18 build",
                "P0: X5 U-Boot loads X5 Image/DTB and mounts the fnOS rootfs by UUID",
                "P0: systemd reaches multi-user, wired DHCP/static network works",
                "P0: SMB share round-trip passes on a separate test data disk",
                "P1: first-boot resize is adapted and observed on a clone, not on the only recovery card",
                "P1: update path is blocked from boot/SPI/NAND or explicitly adapted",
                "P2: BPU/VPU/GPU and fnOS application integration",
            ],
        }

    # ------------------------------------------------------------------
    def run(self) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        started = _now()
        if self.rootfs_dir is not None:
            image = InputImage(
                source=self.rootfs_dir,
                size=0,
                sha256=_sha256_directory(self.rootfs_dir),
                kind="directory-fixture",
                image_path=self.rootfs_dir,
                decompressed_size=0,
                decompressed_sha256="",
            )
            partitions = {
                "sector_size": 512,
                "table": "fixture-directory",
                "meta": {"table": "fixture-directory"},
                "partitions": [],
                "pre_partition_bytes": 0,
                "unpartitioned_bytes": 0,
            }
        else:
            image = prepare_image(self.image_path, self.work_dir)
            partitions = parse_partitions(image.image_path)
        official = {
            "source_url": self.source_url,
            "checksum_type": "md5" if self.official_md5 else None,
            "checksum": self.official_md5,
            "checksum_verified": None,
        }
        if self.official_md5:
            official["checksum_verified"] = (
                self._md5(image.source) == self.official_md5 if image.source.is_file() else None
            )

        if self.rootfs_dir is not None:
            rootfs = DirectoryBackend(self.rootfs_dir, fstype="fixture")
            root_partition = {"index": 0, "start_bytes": 0, "size_bytes": 0, "role": "rootfs", "filesystem": {"type": "fixture"}}
        else:
            rootfs, root_partition = self._open_rootfs(image, partitions)
        facts: dict = {}
        if rootfs is None:
            self.add_gap("rootfs-unreadable", "rootfs", "no supported root filesystem could be opened read-only")
            self.add_finding(
                "rootfs-unreadable",
                "P0",
                "rootfs",
                "No supported root filesystem could be audited",
                [f"partition table: {partitions.get('table')}"],
                "provide an ext4/squashfs rootfs or add a read-only backend",
            )
            facts = {"rootfs": {"fstype": None}}
        else:
            facts = self._inspect_rootfs(rootfs, partitions, root_partition)
            facts["boot_partition"] = self.inspect_boot_partition(image, partitions, root_partition)
            facts["image"] = {
                "table": partitions.get("table"),
                "pre_partition_bytes": partitions.get("pre_partition_bytes", 0),
                "disk_guid": partitions.get("meta", {}).get("disk_guid"),
                "partition_names": [p.get("name", "") for p in partitions.get("partitions", [])],
            }
            if not official.get("checksum_verified") and self.official_md5:
                self.add_finding(
                    "official-md5-mismatch",
                    "P0",
                    "provenance",
                    "Downloaded artifact does not match the official MD5",
                    [f"expected {self.official_md5}"],
                    "discard and re-download from the official URL",
                )

        modules = facts.get("modules", {})
        packages = facts.get("packages", {})
        storage = facts.get("storage", {})
        boot_part = facts.get("boot_partition", {})
        kcheck = self.kernel_check(
            facts.get("rootfs", {}), packages, modules, storage, boot_part.get("config_text")
        )
        mcompare = self.module_compare(modules)
        if mcompare.get("performed"):
            fnos_names = set()
            for path in modules.get("paths", []):
                fnos_names.add(os.path.basename(path).split(".ko")[0])
            x5 = set(mcompare["x5_modules"])
            mcompare["fnos_count"] = len(fnos_names)
            mcompare["fnos_modules"] = sorted(fnos_names)
            mcompare["intersection"] = sorted(fnos_names & x5)
            mcompare["fnos_only"] = sorted(fnos_names - x5)
            mcompare["x5_only_count"] = len(x5 - fnos_names)
            generic = re.compile(r"^(ext4|btrfs|xfs|overlay|bridge|nf_|nft|usb|uas|md_|dm_|cifs|nfs|crypt|vfat|nls_|bonding|8021q|scsi|sd_|sg|xhci|usbcore|loop|fuse)")
            generic_only = [m for m in mcompare["fnos_only"] if generic.match(m)]
            builtin_map = {
                "uas": "CONFIG_USB_UAS",
                "sg": "CONFIG_CHR_DEV_SG",
                "nfs": "CONFIG_NFS_FS",
                "nfsd": "CONFIG_NFSD",
                "nfsv4": "CONFIG_NFS_V4",
                "cifs": "CONFIG_CIFS",
                "overlay": "CONFIG_OVERLAY_FS",
                "fuse": "CONFIG_FUSE_FS",
                "btrfs": "CONFIG_BTRFS_FS",
            }
            builtin_in_x5 = []
            if self.kernel_config and self.kernel_config.exists():
                cfg = {}
                for line in self.kernel_config.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("CONFIG_") and "=" in line:
                        k, v = line.strip().split("=", 1)
                        cfg[k] = v
                for name in list(generic_only):
                    symbol = builtin_map.get(name)
                    if symbol and cfg.get(symbol) == "y":
                        builtin_in_x5.append(name)
                        generic_only.remove(name)
            mcompare["fnos_only_builtin_in_x5"] = builtin_in_x5
            mcompare["fnos_only_generic"] = generic_only[:200]

        self._derive_findings(facts, kcheck, mcompare)
        result = {
            "schema": SCHEMA,
            "tool": {"name": "fnos-input-audit", "version": TOOL_VERSION},
            "generated_at": _now(),
            "started_at": started,
            "mode": "static-readonly-fixture" if self.rootfs_dir is not None else "static-readonly",
            "safety": {
                "mounted": False,
                "image_code_executed": False,
                "sudo_used": False,
                "disk_written": False,
                "host_tools_used": self.runner.commands,
                "allowed_tools": sorted({"debugfs", "dumpe2fs", "unsquashfs", "btrfs", "zstd"}),
            },
            "input": {**image.as_dict(), "official": official},
            "image": partitions,
            "rootfs": facts,
            "kernel_compare": {"config": kcheck, "modules": mcompare},
            "findings": [f.as_dict() for f in sorted(self.findings, key=lambda f: (f.severity, f.id))],
            "gaps": self.gaps,
            "verdict": self.build_verdict(facts),
            "assembly_interface": self.build_assembly(facts),
        }
        return result

    def _md5(self, path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()

    def _open_rootfs(self, image: InputImage, partitions: dict):
        candidates = []
        for p in partitions.get("partitions", []):
            fstype = p.get("filesystem", {}).get("type")
            if fstype in ("ext4", "ext2/3", "btrfs"):
                candidates.append((p["size_bytes"], p, fstype))
        # Prefer the partition explicitly named rootfs, then the largest filesystem.
        candidates.sort(key=lambda x: (("root" in (x[1].get("name") or "").lower()), x[0]), reverse=True)
        for _size, part, fstype in candidates:
            try:
                if fstype == "btrfs":
                    backend = BtrfsBackend(
                        image.image_path,
                        part["start_bytes"],
                        part["size_bytes"],
                        self.work_dir,
                        self.runner,
                        part["index"],
                    )
                else:
                    backend = Ext4Backend(
                        image.image_path,
                        part["start_bytes"],
                        part["size_bytes"],
                        self.work_dir,
                        self.runner,
                        part["index"],
                        fstype,
                    )
                if backend.exists("/etc/os-release") or (backend.exists("/etc") and backend.exists("/usr")):
                    part["role"] = "rootfs"
                    return backend, part
            except RootFSError as exc:
                self.add_gap(f"rootfs-p{part['index']}", "rootfs", f"{fstype} backend failed: {exc}")
        # unpartitioned squashfs rootfs
        whole = partitions.get("meta", {}).get("whole_image_filesystem", {})
        if whole.get("type") == "squashfs":
            try:
                backend = SquashfsBackend(image.image_path, 0, self.runner)
                if backend.exists("/etc/os-release"):
                    return backend, {"index": 0, "start_bytes": 0, "size_bytes": image.decompressed_size, "role": "rootfs"}
            except RootFSError as exc:
                self.add_gap("rootfs-squashfs", "rootfs", f"squashfs backend failed: {exc}")
        return None, None

    def inspect_boot_partition(self, image: InputImage, partitions: dict, root_partition: dict | None) -> dict:
        """Read the separate /boot partition read-only; never use it for X5."""
        for part in partitions.get("partitions", []):
            if root_partition and part.get("index") == root_partition.get("index"):
                continue
            fs_type = part.get("filesystem", {}).get("type")
            if fs_type not in ("ext4", "ext2/3"):
                continue
            name = (part.get("name") or "").lower()
            if name not in ("boot", "bootfs") and not (100 << 20) <= part.get("size_bytes", 0) <= (2 << 30):
                continue
            try:
                backend = Ext4Backend(
                    image.image_path, part["start_bytes"], part["size_bytes"],
                    self.work_dir, self.runner, part["index"], fs_type,
                )
                names = [e.name for e in backend.listdir("/") if not e.is_dir]
                if not any(n.startswith(("config-", "vmlinuz", "Image", "boot.cmd", "boot.scr")) for n in names):
                    continue
                config_name = next((n for n in names if n.startswith("config-")), None)
                config_text = _read_text(backend, "/" + config_name, 2 * 1024 * 1024) if config_name else None
                boot_cmd = _read_text(backend, "/boot.cmd", 512 * 1024)
                fn_env = _read_text(backend, "/fnEnv.txt", 64 * 1024)
                files = []
                for entry in backend.listdir("/"):
                    if entry.is_dir:
                        continue
                    files.append({"name": entry.name, "size": entry.size, "sha256": None})
                dtb = []
                if backend.exists("/dtb"):
                    dtb = [e.name for e in backend.listdir("/dtb") if not e.is_dir]
                refs = _scan_text("/boot.cmd", boot_cmd or "", ("rockchip", "rk3", "fdt", "booti", "mmc", "load", "fatload", "ext4load"), 30)
                return {
                    "partition_index": part.get("index"),
                    "fstype": fs_type,
                    "files": files,
                    "config_name": config_name,
                    "config_text": config_text,
                    "config_sha256": _sha256_bytes(config_text.encode()) if config_text else None,
                    "boot_cmd_present": boot_cmd is not None,
                    "fn_env": (fn_env or "").strip()[:2000] or None,
                    "dtb_files": dtb,
                    "boot_cmd_references": refs,
                }
            except RootFSError as exc:
                self.add_gap(f"boot-p{part.get('index')}", "boot", f"boot partition backend failed: {exc}")
        return {}

    def check_fstab_identity(self, partitions: dict, root_part: dict, storage: dict) -> dict:
        actual = {}
        if root_part:
            actual["/"] = root_part.get("filesystem", {}).get("uuid")
        for candidate in partitions.get("partitions", []):
            if (candidate.get("name") or "").lower() in ("boot", "bootfs"):
                actual["/boot"] = candidate.get("filesystem", {}).get("uuid")
        mismatches = []
        checked = []
        for entry in storage.get("fstab", []):
            mount = entry.get("mount")
            device = entry.get("device", "")
            if mount not in actual or not device.startswith("UUID="):
                continue
            value = device[5:]
            checked.append({"mount": mount, "fstab_uuid": value, "partition_uuid": actual.get(mount)})
            if actual.get(mount) and value.lower() != actual[mount].lower():
                mismatches.append(checked[-1])
        return {"checked": checked, "mismatches": mismatches}

    def _inspect_rootfs(self, rootfs: RootFS, partitions: dict, part: dict) -> dict:
        text_files = self.collect_text(rootfs)
        os_release_text = _read_text(rootfs, "/etc/os-release", 64 * 1024)
        os_release = _parse_os_release(os_release_text)
        debian_version = _read_text(rootfs, "/etc/debian_version", 4096)
        hostname = _read_text(rootfs, "/etc/hostname", 4096)
        packages = self.analyze_packages(rootfs)
        elf = self.analyze_elf(rootfs)
        systemd = self.analyze_systemd(rootfs)
        boot = self.analyze_boot(rootfs, partitions)
        scripts = self.analyze_scripts(rootfs, text_files)
        scripts["update_binaries"] = self.analyze_update_binaries(rootfs)
        modules = self.analyze_modules(rootfs)
        network = self.analyze_network(rootfs, modules)
        storage = self.analyze_storage(rootfs, packages, text_files)
        identity_checks = self.check_fstab_identity(partitions, part, storage)
        facts = {
            "partition_index": part.get("index"),
            "fstype": rootfs.fstype,
            "backend": rootfs.backend,
            "backend_detail": getattr(rootfs, "restore_info", {}),
            "uuid": part.get("filesystem", {}).get("uuid"),
            "unique_guid": part.get("unique_guid"),
            "label": part.get("filesystem", {}).get("label"),
            "os_release": os_release,
            "debian_version": (debian_version or "").strip() or None,
            "hostname": (hostname or "").strip() or None,
            "packages": packages,
            "elf": elf,
            "systemd": systemd,
            "boot": boot,
            "scripts": scripts,
            "modules": modules,
            "network": network,
            "storage": storage,
            "identity_checks": identity_checks,
            "text_files": [{"path": t["path"], "size": t["size"], "sha256": t["sha256"]} for t in text_files],
        }
        return facts

    # ------------------------------------------------------------------
    def _derive_findings(self, facts: dict, kcheck: dict, mcompare: dict) -> None:
        if not facts.get("fstype"):
            return
        fstype = facts.get("fstype")
        image_meta = facts.get("image", {})
        pre = int(image_meta.get("pre_partition_bytes") or 0)
        boot_names = [
            n for n in image_meta.get("partition_names", [])
            if n and any(k in n.lower() for k in ("uboot", "u-boot", "idbloader", "trust", "resource", "misc"))
        ]
        if pre > 4 * 1024 * 1024 or boot_names:
            self.add_finding(
                "foreign-boot-region",
                "P0",
                "boot",
                "Input contains a pre-partition bootloader region or vendor boot partitions",
                [f"pre-partition bytes: {pre}", f"partition names: {boot_names}"],
                "do not copy these bytes/partitions; build a fresh X5 boot chain from the X5 BSP",
                "observed",
            )
        packages = facts.get("packages", {}).get("installed", {})
        elf = facts.get("elf", {})
        systemd = facts.get("systemd", {})
        init = systemd.get("init", {})
        scripts = facts.get("scripts", {})
        storage = facts.get("storage", {})
        boot = facts.get("boot", {})
        modules = facts.get("modules", {})
        network = facts.get("network", {})
        identity = facts.get("identity_checks", {})
        if identity.get("mismatches"):
            self.add_finding(
                "fstab-uuid-mismatch",
                "P1",
                "rootfs",
                "fstab UUIDs do not match the partition superblock UUIDs",
                [f"{m['mount']}: fstab={m['fstab_uuid']} partition={m['partition_uuid']}" for m in identity["mismatches"]],
                "resolve before assembly; verify root=PARTUUID and regenerate fstab from the assembled image",
                "observed",
            )

        # rootfs / boot chain
        bootpart = facts.get("boot_partition", {})
        boot_evidence = []
        if bootpart:
            boot_evidence.append("boot partition files: " + ", ".join(f.get("name", "") for f in (bootpart.get("files") or [])[:12]))
            if bootpart.get("fn_env"):
                boot_evidence.append("fnEnv: " + bootpart["fn_env"].replace("\n", " "))
            boot_evidence.append(f"config: {bootpart.get('config_name')}")
        self.add_finding(
            "boot-chain-must-be-x5",
            "P0",
            "boot",
            "fnOS boot files are target-specific and cannot be reused for X5",
            boot_evidence or ["no separate boot partition was readable"],
            "regenerate boot.scr/extlinux/initramfs against X5 U-Boot and the same-build 6.18 Image/DTB/modules",
            "engineering",
        )
        if fstype == "btrfs":
            self.add_finding(
                "rootfs-btrfs",
                "P0",
                "rootfs",
                "rootfs is Btrfs; X5 6.18 has Btrfs as a module and an initramfs must load it",
                ["CONFIG_BTRFS_FS=m"],
                "include btrfs in initramfs and verify mount before switching root",
            )
        if not init.get("is_systemd"):
            self.add_finding(
                "systemd-missing",
                "P0",
                "userspace",
                "systemd is not the detected init",
                [json.dumps(init, ensure_ascii=False)],
                "confirm the intended init before assembling the image",
            )
        elif "systemd" not in packages:
            self.add_finding(
                "systemd-pkg-unknown",
                "P1",
                "userspace",
                "systemd binary exists but the systemd package version is not in dpkg status",
                [f"packages seen: {len(packages)}"],
                "export a package manifest from the same image",
            )
        if elf.get("foreign"):
            self.add_finding(
                "foreign-elf",
                "P0",
                "userspace",
                "Non-AArch64 ELF objects were found in the key binary sample",
                [f"{x['path']}: {x['machine']}" for x in elf["foreign"]],
                "replace foreign binaries or confirm they are data-only",
            )
        if not elf.get("loader"):
            self.add_finding(
                "loader-missing",
                "P0",
                "userspace",
                "No AArch64 ELF interpreter was found at the standard loader paths",
                [f"looked for: {', '.join(LOADER_PATHS)}"],
                "inspect the actual PT_INTERP paths of /sbin/init and systemd",
            )
        elif elf["loader"].get("elf") and not elf["loader"]["elf"].get("is_aarch64"):
            self.add_finding(
                "loader-not-aarch64",
                "P0",
                "userspace",
                "ELF interpreter is not AArch64",
                [json.dumps(elf["loader"], ensure_ascii=False)[:500]],
                "replace the loader/libc set with an AArch64 userspace",
            )
        if not elf.get("libc"):
            self.add_finding(
                "libc-missing",
                "P0",
                "userspace",
                "No glibc libc.so.6 was found in the standard multiarch paths",
                [f"looked for: {', '.join(LIBC_PATHS)}"],
                "determine the actual C library and loader before assembly",
            )
        if elf.get("max_glibc_requirement"):
            self.add_finding(
                "glibc-floor",
                "INFO",
                "userspace",
                f"Key binaries require up to {elf['max_glibc_requirement']}",
                [f"libc banner: {elf.get('libc', {}).get('banner')}"],
                "keep libc and loader from the same fnOS userspace",
            )

        # resize / update
        if scripts.get("resize_scripts"):
            self.add_finding(
                "resize-must-be-adapted",
                "P1",
                "first-boot",
                "fnOS contains first-boot resize logic that assumes its own partition layout",
                [s["path"] for s in scripts["resize_scripts"][:8]],
                "map to the X5 partition layout and test on a clone; never on the only recovery card",
            )
        else:
            self.add_gap("resize-not-found", "first-boot", "no resize/first-boot script found in scanned paths; growth behaviour unknown")
        update_bin = scripts.get("update_binaries", {})
        if update_bin.get("assessment") == "binary_may_touch_boot_or_flash":
            self.add_finding(
                "update-binary-flash-strings",
                "P0",
                "update",
                "Updater binaries contain SPI/NAND/U-Boot/flash-related strings",
                [b["path"] for b in update_bin.get("binaries", [])],
                "do not enable this updater on X5 until its boot/flash paths are inspected and disabled",
                "observed",
            )
        elif update_bin.get("assessment") == "binary_boot_aware_no_flash_strings":
            self.add_finding(
                "update-binary-boot-aware",
                "P1",
                "update",
                "Updater binaries are boot-family aware but showed no direct flash strings",
                [b["path"] for b in update_bin.get("binaries", [])],
                "confirm the updater cannot replace X5 boot/kernel; block boot-family changes in the X5 image",
                "observed",
            )
        if scripts.get("assessment") == "flash_or_bootloader_references_found":
            refs = scripts.get("flash_references", [])
            self.add_finding(
                "update-may-touch-boot",
                "P0",
                "update",
                "Scanned scripts reference SPI/NAND/U-Boot/raw flash operations",
                [f"{r['path']}:{r['line']} {r['keyword']}" for r in refs[:10]],
                "disable or adapt these paths before any first boot; updates must not write X5 boot/SPI/NAND",
            )
        elif scripts.get("assessment") == "boot_update_references_found":
            refs = scripts.get("boot_references", [])
            self.add_finding(
                "boot-update-path-needs-adaptation",
                "P1",
                "update",
                "Scanned scripts invoke initramfs/grub/boot update paths that assume the fnOS boot layout",
                [f"{r['path']}:{r['line']} {r['keyword']}" for r in refs[:10]],
                "replace these calls with the X5 BSP boot/initramfs pipeline; do not let fnOS rewrite X5 boot",
            )
        elif scripts.get("assessment") == "update_mechanism_found_no_boot_reference":
            self.add_finding(
                "update-unverified",
                "P1",
                "update",
                "An update mechanism was found but its boot/kernel coverage is not proven",
                [f"references: {len(scripts.get('update_references', []))}"],
                "test upgrade/rollback on a clone and diff boot/SPI/NAND before and after",
            )
        else:
            self.add_gap("update-not-found", "update", "no update path found in scanned text; update behaviour unknown")

        # network / storage
        self.add_finding(
            "network-bringup-unverified",
            "P0",
            "network",
            "Wired network bring-up for X5 is not demonstrated by a static audit",
            [f"managers: {network.get('managers')}", f"nic modules: {network.get('nic_modules')}"],
            "verify X5 Ethernet driver, PHY/DTB and DHCP/static configuration on hardware",
        )
        if not storage.get("samba", {}).get("installed"):
            self.add_finding(
                "samba-not-installed",
                "P1",
                "storage",
                "No Samba package was detected; the P0 SMB service is not present in this input",
                [json.dumps(storage.get("packages", {}), ensure_ascii=False)],
                "confirm whether fnOS ships SMB in another package or installs it after first boot",
            )
        vfs_missing = storage.get("samba", {}).get("vfs_missing") or []
        if vfs_missing:
            self.add_finding(
                "samba-vfs-missing",
                "P0",
                "storage",
                "Samba config references VFS modules that are missing from this userspace",
                vfs_missing,
                "ship the matching Samba VFS modules (for example trim_perms/full_audit) or remove the option",
                "observed",
            )
        elif not storage.get("samba", {}).get("config_present"):
            self.add_finding(
                "smb-conf-missing",
                "P1",
                "storage",
                "Samba is installed but /etc/samba/smb.conf is absent",
                [],
                "confirm the fnOS configuration path before claiming SMB is ready",
            )
        if fstype == "btrfs" and not storage.get("storage_stack", {}).get("btrfs"):
            self.add_finding(
                "btrfs-tools-missing",
                "P0",
                "storage",
                "Btrfs rootfs but no btrfs userspace tool detected",
                [],
                "include btrfs-progs in the assembled userspace",
            )
        if "mdadm" in packages and not storage.get("storage_stack", {}).get("mdadm"):
            self.add_finding("mdadm-tool-missing", "P1", "storage", "mdadm package present but /sbin/mdadm not found", [], "verify RAID support before P1")
        if "nfs-kernel-server" in packages and not kcheck.get("performed", False):
            self.add_gap("nfs-kernel-check", "kernel", "NFS server package present but kernel config was not supplied")

        # board identity
        board_blob = json.dumps(network.get("board_identity", []), ensure_ascii=False).lower()
        boot_blob = json.dumps(boot.get("configs", {}), ensure_ascii=False).lower()
        rk_markers = [m for m in ("rockchip", "rk35", "rk3588", "rk3566", "rk3399", "rk3328") if m in (board_blob + boot_blob)]
        x5_markers = [m for m in ("d-robotics", "sunrise", "hobot", "rdk x5") if m in (board_blob + boot_blob)]
        if rk_markers and not x5_markers:
            self.add_finding(
                "board-identity-rk",
                "P1",
                "board-identity",
                "Board/DTB identification in the input is Rockchip-specific",
                [f"markers: {rk_markers}"],
                "add an explicit X5 identity path; do not spoof a Rockchip model or reuse its DTB",
            )
        elif not x5_markers:
            self.add_gap("board-identity-unknown", "board-identity", "no X5 or Rockchip identity marker found in scanned configs")

        # modules
        if mcompare.get("performed"):
            fnos_v = sorted({v["vermagic"] for v in modules.get("vermagic", [])})
            x5_v = mcompare.get("x5_vermagic", [])
            mcompare["fnos_vermagic"] = fnos_v
            fnos_rel = modules.get("kernelrelease")
            x5_rel = x5_v[0].split()[0] if x5_v else None
            mcompare["fnos_kernelrelease"] = fnos_rel
            mcompare["x5_kernelrelease"] = x5_rel
            if fnos_rel and x5_rel and fnos_rel != x5_rel:
                self.add_finding(
                    "kernelrelease-mismatch",
                    "P0",
                    "kernel-modules",
                    "fnOS userland modules are built for a different kernelrelease than X5 6.18",
                    [f"fnOS: {fnos_rel}", f"X5: {x5_rel}"],
                    "rebuild every required module against the X5 6.18.0 tree and regenerate the initramfs",
                    "observed",
                )
            if fnos_v and x5_v and not (set(fnos_v) & set(x5_v)):
                self.add_finding(
                    "module-vermagic-mismatch",
                    "P0",
                    "kernel-modules",
                    "fnOS modules and X5 6.18 modules have different vermagic; they cannot be mixed",
                    [f"fnOS: {fnos_v}", f"X5: {x5_v}"],
                    "rebuild every required module against the X5 6.18.0 tree; do not copy fnOS .ko files",
                    "observed",
                )
            generic_only = mcompare.get("fnos_only_generic", [])
            if generic_only:
                self.add_finding(
                    "module-inventory-diff",
                    "P2",
                    "kernel-modules",
                    "fnOS module inventory differs from the X5 6.18 build",
                    [f"fnOS-only generic names: {', '.join(generic_only[:20])}"],
                    "map every required storage/network module to the 6.18 config; do not copy fnOS .ko files",
                )
        else:
            self.add_gap("module-compare-missing", "kernel-modules", "no X5 6.18 modules directory supplied")
