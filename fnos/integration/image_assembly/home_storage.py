"""Embed the explicitly requested household storage policy in an offline image.

Only fnOS storage services receive the compatibility library. The library
selects mounted /volN data volumes at runtime; OS paths keep native behavior.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .platform_runtime import _destination, _prepare_parent

UNITS = (
    # triminit mounts existing data volumes before the interactive services.
    # It needs the same mount compatibility as trim_main on every cold boot.
    "trim_init", "trim_main", "filestor_service", "finder_service", "trim_upload",
    "imagesrv", "sysinfo_service", "trim_file_monitor", "share_service",
    "smbd", "winbind", "smbftpd", "trim_app_center", "mediasrv", "ai_manager",
    "backup_service", "dlcenter", "multiple-downloads", "sysrestore",
    "trim_open_gateway", "trim_sharelink", "eventlogger_service",
    "auto_thumbnailer", "cloud_storage_dav",
)
MANIFEST = Path("usr/share/fnos-x5/home-storage-manifest.json")


def _arm_library(library: Path) -> bytes:
    data = library.read_bytes()
    if (data[:6] != b"\x7fELF\x02\x01" or len(data) < 64
            or int.from_bytes(data[16:18], "little") != 3
            or int.from_bytes(data[18:20], "little") != 183):
        raise ValueError("home storage bridge must be an AArch64 ELF library")
    return data


def stage_home_storage(root: Path, library: Path, ftp_library: Path | None = None,
                       *, enable_cloud_cache=False) -> dict:
    root = root.resolve()
    if root == Path("/") or not (root / "etc/os-release").is_file():
        raise ValueError("offline root filesystem required")
    data = _arm_library(library)
    ftp_data = _arm_library(ftp_library) if ftp_library else None
    digest = hashlib.sha256(data).hexdigest()
    target = f"usr/lib/fnos-x5/libhome_acl-{digest[:16]}.so"
    plan = {
        target: data,
        "etc/fnos-x5/home-storage.env": (
            "# User-selected household policy; mounted fnOS data volumes only.\n"
            f"LD_PRELOAD=/{target}\nFNOS_HOME_DATA_ROOT=auto\n"
        ).encode(),
        "usr/lib/fnos-x5/app_environment.py": Path(__file__).with_name("app_environment.py").read_bytes(),
        "usr/lib/fnos-x5/app_center_compat.py": Path(__file__).with_name("app_center_compat.py").read_bytes(),
        "usr/lib/fnos-x5/storage_namespaces.py": Path(__file__).with_name("storage_namespaces.py").read_bytes(),
        "etc/systemd/system/fnos-home-storage.service": (
            "[Unit]\nDescription=Prepare household storage namespace parents\n"
            "After=local-fs.target\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            "ExecStart=/usr/bin/python3 /usr/lib/fnos-x5/storage_namespaces.py\n"
        ).encode(),
    }
    for unit in UNITS:
        plan[f"etc/systemd/system/{unit}.service.d/80-fnos-home-storage.conf"] = (
            "[Unit]\nRequires=fnos-home-storage.service\nAfter=fnos-home-storage.service\n"
            "[Service]\nEnvironmentFile=/etc/fnos-x5/home-storage.env\n"
        ).encode()
    plan["etc/systemd/system/trim_app_center.service.d/80-fnos-home-storage.conf"] += (
        "ExecStartPre=/usr/bin/python3 /usr/lib/fnos-x5/app_center_compat.py --apply\n"
        "ExecStartPre=/usr/bin/python3 /usr/lib/fnos-x5/app_environment.py\n"
        "ExecStartPost=/usr/bin/python3 /usr/lib/fnos-x5/app_environment.py --wait 30\n"
    ).encode()
    if ftp_data is not None:
        ftp_digest = hashlib.sha256(ftp_data).hexdigest()
        ftp_target = f"usr/lib/fnos-x5/libftp_storage-{ftp_digest[:16]}.so"
        plan[ftp_target] = ftp_data
        plan["etc/fnos-x5/ftp-storage.env"] = (
            "# Native FTP authentication; isolated data-volume view per login.\n"
            f"LD_PRELOAD=/{ftp_target}:/{target}\nFNOS_HOME_DATA_ROOT=auto\n"
        ).encode()
        plan["etc/systemd/system/smbftpd.service.d/80-fnos-home-storage.conf"] = (
            "[Unit]\nRequires=fnos-home-storage.service\nAfter=fnos-home-storage.service\n"
            "[Service]\nEnvironmentFile=/etc/fnos-x5/ftp-storage.env\n"
        ).encode()
    # The native daemon consumes requests from the proprietary kernel trash
    # device. On this portable kernel, home_trash handles native unlinkat calls
    # in the storage services instead; do not restart a missing device forever.
    plan["etc/systemd/system/trim_trashbind.service.d/80-fnos-x5-portable-trash.conf"] = (
        "[Unit]\nConditionPathExists=/dev/trim-trashbin\n"
        "[Service]\nEnvironmentFile=/etc/fnos-x5/home-storage.env\n"
    ).encode()
    cloud_report = None
    if enable_cloud_cache:
        from .cloud_storage_compat import patched
        # Reject unknown binaries before any staging writes.
        native = _destination(root, Path("usr/trim/bin/cloud_storage_dav"))
        patched(native.read_bytes())
        plan["usr/lib/fnos-x5/cloud_storage_cache.py"] = Path(__file__).with_name("cloud_storage_cache.py").read_bytes()
        plan["etc/systemd/system/cloud_storage_dav.service.d/90-fnos-persistent-cache.conf"] = (
            "[Unit]\nStartLimitIntervalSec=0\n"
            "[Service]\nExecStart=\n"
            "ExecStart=/usr/bin/python3 /usr/lib/fnos-x5/cloud_storage_cache.py\n"
            "TimeoutStopSec=30\nRestart=always\nRestartSec=5\n"
        ).encode()
    # Preserve each service's normal umask: no blanket permission change to OS
    # files, package metadata, account configuration or credential files.
    report = {
        "schema": "fnos-home-storage/v1", "policy": "household-shared-mounted-data-volumes",
        "units": list(UNITS), "library_sha256": digest,
        "ftp_view": "private-mount-namespace-chroot" if ftp_data is not None else "not-staged",
        "files": [{"path": name, "sha256": hashlib.sha256(payload).hexdigest()}
                  for name, payload in plan.items()],
        "acceptance": "packaging does not imply native workflow acceptance",
    }
    plan[str(MANIFEST)] = (json.dumps(report, indent=2) + "\n").encode()
    for name in plan:
        if _destination(root, Path(name)).is_symlink():
            raise ValueError(f"symlink at policy destination: {name}")
    if enable_cloud_cache:
        from .cloud_storage_compat import install
        cloud_report = install(root)
        report["cloud_storage"] = cloud_report
        for name in ("usr/trim/bin/cloud_storage_dav",
                     "usr/lib/fnos-x5/vendor-original/cloud_storage_dav-" + cloud_report["original_sha256"]):
            report["files"].append({"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()})
        plan[str(MANIFEST)] = (json.dumps(report, indent=2) + "\n").encode()
    for name, payload in plan.items():
        dst = root / name
        _prepare_parent(root, dst.parent)
        dst.write_bytes(payload)
        dst.chmod(0o644)
    verify_home_storage(root, report)
    return report


def verify_home_storage(root: Path, report: dict, *, require_root_owner=False) -> dict:
    root = root.resolve()
    manifest = _destination(root, MANIFEST)
    if manifest.is_symlink() or json.loads(manifest.read_text()) != report:
        raise ValueError("home storage manifest mismatch")
    for row in report["files"]:
        p = _destination(root, Path(row["path"]))
        if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError(f"home storage file mismatch: {row['path']}")
    if require_root_owner:
        for p in [manifest, *(root / row["path"] for row in report["files"])]:
            while p != root:
                st = p.lstat()
                if st.st_uid or st.st_gid or st.st_mode & 0o022:
                    raise ValueError(f"unsafe home storage runtime metadata: {p.relative_to(root)}")
                p = p.parent
    return {"verified_files": len(report["files"]), "units": len(report["units"])}
