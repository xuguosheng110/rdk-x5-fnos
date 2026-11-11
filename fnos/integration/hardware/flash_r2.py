#!/usr/bin/env python3
"""Reviewed r2 image write + exact-image readback handoff.

Uses the existing fnos.lab FlashBackend/ImageHandle primitives.  Default is a
preflight only; --execute requires root, a Codex acceptance artifact with
accepted=true, an exact reviewed UMS target, and a verified whole-card backup.
It does not power-cycle or boot the board; booting remains in the reviewed
single-session safe-cut workflow.
"""
from __future__ import annotations
import argparse, hashlib, json, os, plistlib, shlex, subprocess, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from fnos.lab.errors import Refused, HardwareFailure
from fnos.lab.flash import MacDiskutilBackend, open_trusted_image, readback_image, require_full_readback, write_image
from fnos.lab.locks import board_lock
from fnos.lab.uart_session import SessionLock
from fnos.integration.hardware import macos_identity
SSH_SOCKET="/home/your-user/.cache/rdk-fnos-ssh/build-server.sock"; SSH_HOST="guosheng@192.0.2.10"; BOARD="x5-lab-01"
BOARD_LOCK_ROOT="/home/your-user/.local/state/fnos-lab"
def run(argv,**kw): return subprocess.run(argv,capture_output=True,text=True,timeout=kw.pop("timeout",30),**kw)
def remote(script,*,timeout=60):
    return run(["ssh","-o","ControlMaster=no","-o",f"ControlPath={SSH_SOCKET}","-o","BatchMode=yes","-o","ConnectTimeout=8",SSH_HOST,script],timeout=timeout)
def load_acceptance(path):
    raw=json.loads(Path(path).read_text())
    if raw.get("accepted") is not True: raise SystemExit("acceptance.accepted must be true; reviewed alone is not acceptance")
    sh=raw.get("image_sha256") or (raw.get("candidate") or {}).get("sha256")
    sz=raw.get("image_size") or (raw.get("image_size_bytes") or (raw.get("candidate") or {}).get("size"))
    if not isinstance(sh,str) or not isinstance(sz,int) or sz<=0: raise SystemExit("acceptance lacks exact image sha256/size")
    return sh.lower(),sz
def check_target(device, expect_bytes, expect_model, reviewed, *, require_initial_layout=True):
    facts=macos_identity.live(device)
    if facts["size_bytes"] != expect_bytes: raise SystemExit(f"target size {facts['size_bytes']} != {expect_bytes}")
    if facts["model"] != expect_model: raise SystemExit(f"target model {facts['model']!r} != {expect_model!r}")
    reviewed=macos_identity.reviewed_facts(reviewed)
    macos_identity.compare_stable(reviewed, facts)
    if require_initial_layout:
        macos_identity.compare_initial_layout(facts, reviewed)
    return facts

def validate_lengths(image_size, card_size, backup_bytes):
    if image_size <= 0 or card_size <= 0 or backup_bytes <= 0:
        raise SystemExit("image/card/backup lengths must be positive")
    if image_size > card_size:
        raise SystemExit("image longer than card")
    if backup_bytes != card_size:
        raise SystemExit("backup byte length != card capacity")

def _stat_hash(text, expected_size, expected_sha, label):
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    if len(lines)<2: raise SystemExit(f"{label}: missing stat/hash output")
    try: size=int(lines[0])
    except ValueError: raise SystemExit(f"{label}: first line is not exact size")
    token=lines[1].split()[0] if lines[1].split() else ""
    if size != expected_size: raise SystemExit(f"{label}: size {size} != {expected_size}")
    if token != expected_sha.lower(): raise SystemExit(f"{label}: hash {token!r} != {expected_sha.lower()!r}")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--device",required=True)
    ap.add_argument("--image",required=True,type=Path)
    ap.add_argument("--image-remote",required=True)
    ap.add_argument("--image-sha256",required=True)
    ap.add_argument("--image-size",required=True,type=int)
    ap.add_argument("--acceptance",required=True,type=Path)
    ap.add_argument("--reviewed-target",required=True,type=Path)
    ap.add_argument("--backup-remote",required=True)
    ap.add_argument("--backup-sha256",required=True)
    ap.add_argument("--backup-bytes",required=True,type=int)
    ap.add_argument("--card-bytes",type=int,default=31914983424)
    ap.add_argument("--expect-model",default="UMS disk 0")
    ap.add_argument("--serial-port",default="/dev/cu.usbserial-13220")
    ap.add_argument("--execute",action="store_true")
    args=ap.parse_args()
    if not args.image_sha256 or not args.image_size or args.image_size<=0: raise SystemExit("image hash/size required")
    validate_lengths(args.image_size,args.card_bytes,args.backup_bytes)
    acc_sha,acc_size=load_acceptance(args.acceptance)
    if acc_sha!=args.image_sha256.lower() or acc_size!=args.image_size:
        raise SystemExit("acceptance image hash/size differs from arguments")
    reviewed=json.loads(args.reviewed_target.read_text())
    if reviewed.get("reviewed_mapping") is not True: raise SystemExit("reviewed target mapping is not accepted")
    backend=MacDiskutilBackend(args.device)
    actual=check_target(args.device,args.card_bytes,args.expect_model,reviewed)
    # Verify the whole-card backup on the server before looking at the target.
    backup=remote(f"set -e; test -f {shlex.quote(args.backup_remote)}; stat -c%s {shlex.quote(args.backup_remote)}; sha256sum {shlex.quote(args.backup_remote)}",timeout=600)
    if backup.returncode!=0:
        raise SystemExit(f"backup missing/unreadable: {backup.stdout[-200:]}")
    _stat_hash(backup.stdout,args.backup_bytes,args.backup_sha256,"backup")
    image_remote=remote(f"set -e; test -f {shlex.quote(args.image_remote)}; stat -c%s {shlex.quote(args.image_remote)}; sha256sum {shlex.quote(args.image_remote)}",timeout=600)
    if image_remote.returncode!=0:
        raise SystemExit(f"remote image missing/unreadable: {image_remote.stdout[-200:]}")
    _stat_hash(image_remote.stdout,args.image_size,args.image_sha256,"remote image")
    plan={"image":{"path":str(args.image),"sha256":args.image_sha256.lower(),"size_bytes":args.image_size},
          "target":{"device":args.device,"serial":None,"model":args.expect_model,"size_bytes":args.card_bytes,
                    "bus":"USB","topology":":".join(actual["usb_path"])},
          "preconditions":{"whole_disk":True,"internal_or_system_disk":False,"mounted":False,"read_only":False}}
    image=open_trusted_image(plan)
    if image.size_bytes != args.image_size or image.sha256 != args.image_sha256.lower():
        image.close(); raise SystemExit("local image file hash/size differs from arguments")
    print(json.dumps({"ok":True,"facts":actual,"image":str(args.image),"image_sha256":image.sha256,
                      "image_size":image.size_bytes,"card_size":args.card_bytes,"backup":args.backup_remote,
                      "execute":args.execute,"boot":"not performed: use reviewed single-session safe-cut workflow"},ensure_ascii=False,indent=2))
    if not args.execute:
        image.close(); return 0
    if os.geteuid()!=0:
        image.close(); raise SystemExit("--execute requires root; no raw device was opened")
    with board_lock(Path(BOARD_LOCK_ROOT),BOARD) as held:
        port_lock=SessionLock(Path(BOARD_LOCK_ROOT),args.serial_port)
        port_lock.acquire()
        try:
            # Re-identify immediately before opening/writing.
            check_target(args.device,args.card_bytes,args.expect_model,reviewed)
            fd=backend.open_target()
            try:
                write=write_image(backend,fd,image,cancel=lambda:False)
                if not write.complete or write.bytes_written!=image.size_bytes:
                    raise HardwareFailure("write_incomplete",f"wrote {write.bytes_written}/{image.size_bytes}")
                backend.sync()
                check_target(args.device,args.card_bytes,args.expect_model,reviewed,require_initial_layout=False)
                actual_sha,read,complete=readback_image(backend,fd,image,cancel=lambda:False)
                require_full_readback(actual_sha,image,bytes_read=read,complete=complete)
                check_target(args.device,args.card_bytes,args.expect_model,reviewed,require_initial_layout=False)
            finally:
                backend.close_target(fd)
            image.close()
        finally:
            port_lock.release()
    print(json.dumps({"ok":True,"image_sha256":args.image_sha256.lower(),"readback_sha256":actual_sha,"bytes":read,
                      "target":args.device,"note":"write/readback complete; no power/boot action performed"},indent=2))
    return 0
if __name__=="__main__": sys.exit(main())
