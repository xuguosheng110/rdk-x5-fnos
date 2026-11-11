"""macOS UMS whole-disk identity bound to its USB ancestor tree.

The disk number (/dev/diskN) is only a current handle.  Stable review facts are
the live USB gadget (VID/PID/location/serial), its physical ancestor path, the
exact capacity and the on-card partition geometry, all read from diskutil and
IORegistry without root.
"""
from __future__ import annotations
import json, plistlib, subprocess
from pathlib import Path

BOARD_LOCK_ROOT = "/home/your-user/.local/state/fnos-lab"
REQUIRED_REVIEW = ("reviewed_mapping", "usb_gadget_serial", "usb_vid", "usb_pid",
                   "usb_location_id", "usb_path", "model", "size_bytes",
                   "partition_offset_bytes", "partition_size_bytes")

def _run(argv):
    p=subprocess.run(argv,capture_output=True,text=True,timeout=20)
    if p.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed: {p.stderr.strip()}")
    return p

def disk_info(device):
    return plistlib.loads(_run(["diskutil","info","-plist",device]).stdout)

def disk_list(device):
    return plistlib.loads(_run(["diskutil","list","-plist",device]).stdout)

def _walk(node, ancestors=()):
    if isinstance(node, dict):
        yield node, ancestors
        for child in node.get("IORegistryEntryChildren", []) or []:
            yield from _walk(child, ancestors + (node,))
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child, ancestors)

def _label(node):
    name = node.get("IORegistryEntryName") or node.get("USB Product Name") or node.get("BSD Name")
    loc = node.get("IORegistryEntryLocation")
    return f"{name}@{loc}" if loc is not None else str(name)

def usb_binding(disk_name):
    tree=plistlib.loads(_run(["ioreg","-a","-p","IOService","-l","-w0","-c","IOUSBHostDevice"]).stdout)
    found=None
    for node, ancestors in _walk(tree):
        if node.get("BSD Name") == disk_name:
            found=(node, ancestors); break
    if found is None:
        raise RuntimeError(f"IORegistry has no IOMedia node for {disk_name}")
    node, ancestors=found
    gadget=None
    for a in reversed(ancestors):
        if a.get("USB Serial Number") and a.get("USB Product Name"):
            gadget=a; break
    if gadget is None:
        raise RuntimeError(f"{disk_name} has no USB gadget ancestor")
    path=_label(gadget)
    # Full ancestor path makes a different physical port distinguishable.
    full=[]
    for a in ancestors:
        if a is gadget: full.append(path); break
        full.append(_label(a))
    return {
        "usb_product": gadget.get("USB Product Name"),
        "usb_vendor": gadget.get("USB Vendor Name"),
        "usb_gadget_serial": gadget.get("USB Serial Number"),
        "usb_vid": gadget.get("idVendor"),
        "usb_pid": gadget.get("idProduct"),
        "usb_location_id": gadget.get("locationID"),
        "usb_path": full,
        "usb_entry_id": gadget.get("IORegistryEntryID"),
        "disk_ioreg_path": [_label(x) for x in ancestors] + [_label(node)],
    }

def live(device):
    name=Path(device).name
    import re
    if not re.fullmatch(r"disk[0-9]+", name):
        raise RuntimeError("device must be a whole /dev/diskN")
    info=disk_info(device); listing=disk_list(device)
    disks=[d for d in listing.get("AllDisksAndPartitions",[]) if d.get("DeviceIdentifier")==name]
    if len(disks)!=1: raise RuntimeError("diskutil list did not identify exactly one whole disk")
    parts=disks[0].get("Partitions",[]) or []
    mounts=set()
    for part in parts:
        if isinstance(part,dict) and part.get("MountPoint"): mounts.add(part["MountPoint"])
    if info.get("MountPoint"): mounts.add(info["MountPoint"])
    part=parts[0] if len(parts)==1 else None
    part_info=disk_info(f"/dev/{part['DeviceIdentifier']}") if part and part.get("DeviceIdentifier") else {}
    usb=usb_binding(name)
    return {
        "device": device,
        "device_node": info.get("DeviceNode"),
        "model": info.get("MediaName") or info.get("DeviceModel"),
        "serial": info.get("SerialNumber") or info.get("MediaSerialNumber"),
        "size_bytes": info.get("TotalSize"),
        "whole": info.get("WholeDisk") is True,
        "internal": info.get("Internal") is not False,
        "bus": info.get("BusProtocol"),
        "virtual": info.get("VirtualOrPhysical"),
        "read_only": bool(info.get("ReadOnlyMedia") or info.get("MediaReadOnly") or info.get("Writable") is False),
        "mounts": sorted(mounts),
        "partition_count": len(parts),
        "partition_offset_bytes": part_info.get("PartitionMapPartitionOffset"),
        "partition_size_bytes": part_info.get("TotalSize"),
        "partition_type": part.get("Content") if part else None,
        **usb,
    }

def reviewed_facts(value):
    if not isinstance(value,dict): raise ValueError("reviewed mapping must be an object")
    missing=[k for k in REQUIRED_REVIEW if k not in value]
    if missing: raise ValueError("reviewed mapping missing: " + ", ".join(missing))
    if value.get("reviewed_mapping") is not True:
        raise ValueError("reviewed_mapping must be true")
    for k in ("usb_gadget_serial","usb_path","model"):
        if not value.get(k): raise ValueError(f"reviewed {k} is empty")
    for k in ("usb_vid","usb_pid","usb_location_id","size_bytes","partition_offset_bytes","partition_size_bytes"):
        if not isinstance(value.get(k),int) or value[k] <= 0: raise ValueError(f"reviewed {k} must be a positive integer")
    return {k:value[k] for k in REQUIRED_REVIEW}

def compare_stable(reviewed, actual):
    """Stable USB/card identity; valid before and after the image layout changes."""
    checks={
        "reviewed_mapping": True,
        "usb_gadget_serial": actual["usb_gadget_serial"],
        "usb_vid": actual["usb_vid"],
        "usb_pid": actual["usb_pid"],
        "usb_location_id": actual["usb_location_id"],
        "usb_path": actual["usb_path"],
        "model": actual["model"],
        "size_bytes": actual["size_bytes"],
    }
    for key,want in checks.items():
        got=reviewed.get(key)
        if got != want:
            raise ValueError(f"reviewed {key} != live binding ({got!r} != {want!r})")
    if actual["size_bytes"] != 31914983424: raise ValueError("unexpected card capacity")
    if not actual["whole"] or actual["internal"] or (actual["bus"] or "").lower()!="usb" or actual["virtual"]!="Physical":
        raise ValueError("target is not a whole external physical USB disk")
    if actual["mounts"]: raise ValueError("target/child volume is mounted")
    if actual["read_only"]: raise ValueError("target is read-only")
    if not actual["model"]: raise ValueError("model is missing")
    return actual

def compare_initial_layout(actual, reviewed=None, *, expected_offset=4194304, expected_part_size=31910789120):
    """Only the pre-write card must still have the reviewed single FAT p1."""
    if reviewed is not None:
        if reviewed.get("partition_offset_bytes") != actual["partition_offset_bytes"]:
            raise ValueError("reviewed p1 offset differs from live")
        if reviewed.get("partition_size_bytes") != actual["partition_size_bytes"]:
            raise ValueError("reviewed p1 size differs from live")
    if actual["partition_offset_bytes"] != expected_offset: raise ValueError("unexpected p1 offset")
    if actual["partition_size_bytes"] != expected_part_size: raise ValueError("unexpected p1 size")
    if actual["partition_count"] != 1: raise ValueError("expected exactly one p1 and no p2")

def compare(reviewed, actual, *, expected_offset=4194304, expected_part_size=31910789120):
    compare_stable(reviewed,actual)
    compare_initial_layout(actual,reviewed,expected_offset=expected_offset,expected_part_size=expected_part_size)
    return actual

def from_evidence(path):
    """Create a reviewed record from a current live evidence JSON (for Codex)."""
    raw=json.loads(Path(path).read_text())
    out={k:raw.get(k) for k in REQUIRED_REVIEW}
    out["reviewed_mapping"]=raw.get("reviewed_mapping") is True
    return out
