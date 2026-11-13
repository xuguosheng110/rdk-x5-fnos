#!/usr/bin/env python3
"""Audited read-only whole-card backup to the build server.

This is intentionally a thin wrapper around diskutil identity plus a streaming
read.  It refuses ambiguous/target-changed devices, holds the board lock for
the whole read, checks destination exclusivity and free space, hashes the
source stream locally and the saved file remotely, and re-identifies the source
after the read.  No write to the source, no unmount, no sudo guessing.
"""
from __future__ import annotations
import argparse, hashlib, json, os, plistlib, shlex, subprocess, sys, time, zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from fnos.lab.locks import board_lock
from fnos.lab.uart_session import SessionLock
from fnos.integration.hardware import macos_identity
SSH_SOCKET="/home/your-user/.cache/rdk-fnos-ssh/build-server.sock"; SSH_HOST="guosheng@192.0.2.10"
BOARD="x5-lab-01"; EXPECTED=31914983424
BOARD_LOCK_ROOT="/home/your-user/.local/state/fnos-lab"
def run(argv,**kw):
    return subprocess.run(argv,capture_output=True,text=True,timeout=kw.pop("timeout",30),**kw)
def ssh(script,*,timeout=60):
    return run(["ssh","-o","ControlMaster=no","-o",f"ControlPath={SSH_SOCKET}","-o","BatchMode=yes","-o","ConnectTimeout=8",SSH_HOST,script],timeout=timeout)
def info(dev):
    p=run(["diskutil","info","-plist",dev]);
    if p.returncode: raise SystemExit(f"diskutil info failed: {p.stderr.strip()}")
    return plistlib.loads(p.stdout)
def listinfo(dev):
    p=run(["diskutil","list","-plist",dev])
    return plistlib.loads(p.stdout) if p.returncode==0 else {}
def mounts(dev):
    data=listinfo(dev); found=set()
    def walk(x):
        if isinstance(x,dict):
            for k,v in x.items():
                if k in ("MountPoint","MountPoints"):
                    if isinstance(v,str) and v: found.add(v)
                    elif isinstance(v,list): found.update(y for y in v if isinstance(y,str) and y)
                else: walk(v)
        elif isinstance(x,list):
            for y in x: walk(y)
    walk(data); return tuple(sorted(found))
def facts(dev):
    return macos_identity.live(dev)

def validate(f,expected,model):
    p=[]
    if f["size_bytes"] != expected: p.append(f"size {f['size_bytes']} != {expected}")
    if f["internal"] is not False: p.append("not external/internal unknown")
    if f["whole"] is not True: p.append("not whole disk")
    if (f["bus"] or "").lower()!="usb": p.append("not USB")
    if f["virtual"] != "Physical": p.append("virtual media")
    if f["mounts"]: p.append("target/child mounted")
    if f["model"] != model: p.append(f"model {f['model']!r} != {model!r}")
    if not f.get("usb_gadget_serial"): p.append("USB gadget serial missing")
    if not f.get("usb_path"): p.append("USB topology path missing")
    if f.get("partition_offset_bytes") != 4194304: p.append("p1 offset not 4194304")
    if f.get("partition_size_bytes") != 31910789120: p.append("p1 size not 31910789120")
    if f.get("partition_count") != 1: p.append("not exactly one partition")
    return p

def same_identity(a,b):
    keys=("device_node","model","size_bytes","usb_gadget_serial","usb_vid","usb_pid",
          "usb_location_id","usb_path","partition_offset_bytes","partition_size_bytes","partition_count")
    return all(a.get(k)==b.get(k) for k in keys)

def new_gzip_compressor():
    return zlib.compressobj(level=1, method=zlib.DEFLATED, wbits=31)

def stream_ok(dd_rc, recv_rc, total, expected_bytes):
    return dd_rc == 0 and recv_rc == 0 and total == expected_bytes

def verify_remote_text(text, expected_bytes, local_sha):
    # Real producer output is exactly three lines: sha256(partial), sha256(final),
    # exact final size.  Length of partial is checked remotely by ``test``.
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    try:
        hash_partial=lines[0].split()[0]; hash_final=lines[1].split()[0]; size_final=int(lines[2])
    except (IndexError,ValueError):
        raise ValueError("remote verification output malformed")
    if hash_partial!=local_sha or hash_final!=local_sha or size_final!=expected_bytes:
        raise ValueError("source/remote length/hash mismatch")
    return hash_final

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--device",required=True)
    ap.add_argument("--expect-bytes",type=int,default=EXPECTED)
    ap.add_argument("--expect-model",default="UMS disk 0")
    ap.add_argument("--remote-dir",default="/srv/fnos-build/backups/20261106-sd")
    ap.add_argument("--remote-name",default="sd-current-20261106.img")
    ap.add_argument("--evidence",type=Path,default=Path("fnos/logs/uboot-diagnosis-20261106/sd-backup-evidence.json"))
    ap.add_argument("--serial-port",default="/dev/cu.usbserial-13220")
    ap.add_argument("--execute",action="store_true")
    args=ap.parse_args()
    if not args.device.startswith("/dev/disk"):
        raise SystemExit("device must be a whole /dev/diskN")
    before=facts(args.device); problems=validate(before,args.expect_bytes,args.expect_model)
    if problems:
        print(json.dumps({"ok":False,"facts":before,"problems":problems},ensure_ascii=False,indent=2)); return 2
    remote_path=f"{args.remote_dir}/{args.remote_name}"; partial=f"{remote_path}.partial-{os.getpid()}"
    space=ssh(f"set -e; test -d {shlex.quote(args.remote_dir)}; test ! -e {shlex.quote(remote_path)}; test ! -e {shlex.quote(partial)}; df -B1 --output=avail {shlex.quote(args.remote_dir)} | tail -1")
    if space.returncode!=0:
        print(json.dumps({"ok":False,"facts":before,"reason":"remote destination exists or unavailable"},indent=2)); return 2
    try: avail=int(space.stdout.strip().splitlines()[-1])
    except Exception: avail=0
    if avail < args.expect_bytes + 512*1024*1024:
        print(json.dumps({"ok":False,"facts":before,"reason":"remote free space insufficient","avail":avail},indent=2)); return 2
    sudo=run(["sudo","-n","true"])
    plan={"ok":True,"facts":before,"remote_path":remote_path,"partial":partial,"remote_free":avail,
          "method":"board lock + source-stream SHA-256 + remote exact size/hash + exclusive new file",
          "execute":args.execute}
    print(json.dumps(plan,ensure_ascii=False,indent=2))
    if not args.execute: return 0
    if sudo.returncode!=0:
        print(json.dumps({"ok":False,"facts":before,"reason":"local sudo -n unavailable; no raw read performed"},indent=2)); return 2
    with board_lock(Path(BOARD_LOCK_ROOT),BOARD) as held:
        held.revalidate()
        port_lock=SessionLock(Path(BOARD_LOCK_ROOT),args.serial_port)
        port_lock.acquire()
        try:
            inside=facts(args.device)
            if validate(inside,args.expect_bytes,args.expect_model) or not same_identity(before,inside):
                print(json.dumps({"ok":False,"reason":"target changed after lock","before":before,"inside":inside},indent=2)); return 2
            raw=args.device.replace("/dev/disk","/dev/rdisk",1)
            dd=subprocess.Popen(["sudo","-n","dd",f"if={raw}","bs=16m"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            # noclobber opens a new remote file exclusively; no shell race.
            # Network bytes are gzip level-1; the remote decompressor writes an
            # exclusive raw partial.  The source hash is always over raw bytes.
            recv=subprocess.Popen(["ssh","-o","ControlMaster=no","-o",f"ControlPath={SSH_SOCKET}","-o","BatchMode=yes",SSH_HOST,
                                   f"set -C; umask 077; gzip -d > {shlex.quote(partial)}"],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            h=hashlib.sha256(); total=0; network=0; last_progress=time.monotonic(); comp=new_gzip_compressor()
            try:
                while True:
                    chunk=dd.stdout.read(1024*1024)
                    if not chunk: break
                    h.update(chunk); total+=len(chunk)
                    packed=comp.compress(chunk)
                    if packed: recv.stdin.write(packed); network+=len(packed)
                    if time.monotonic()-last_progress>=30:
                        print(json.dumps({"progress":True,"raw_bytes":total,"network_bytes":network,
                                          "expected_raw":args.expect_bytes,
                                          "percent":round(total*100/args.expect_bytes,2)},indent=2),flush=True)
                        last_progress=time.monotonic()
                    held.revalidate()
                tail=comp.flush()
                if tail: recv.stdin.write(tail); network+=len(tail)
                recv.stdin.close(); dd.wait(timeout=60); recv.wait(timeout=60)
                drc, rrc = dd.returncode, recv.returncode
                err=recv.stderr.read().decode("utf-8","replace") if recv.stderr else ""
            except Exception:
                for proc in (dd,recv):
                    try: proc.kill()
                    except Exception: pass
                raise
            after=facts(args.device)
            if not stream_ok(drc,rrc,total,args.expect_bytes):
                print(json.dumps({"ok":False,"dd_rc":drc,"recv_rc":rrc,"bytes":total,"error":err[-300:]},indent=2)); return 3
            if validate(after,args.expect_bytes,args.expect_model) or not same_identity(before,after):
                print(json.dumps({"ok":False,"reason":"source identity changed during read","before":before,"after":after},indent=2)); return 4
            # The remote shell checks exact length and hash BEFORE link(2)
            # publishes atomically; a failed check leaves the partial in place.
            remote=ssh(f"set -e; test $(stat -c%s {shlex.quote(partial)}) -eq {args.expect_bytes}; test \"$(sha256sum {shlex.quote(partial)} | cut -d' ' -f1)\" = {h.hexdigest()}; sha256sum {shlex.quote(partial)}; ln {shlex.quote(partial)} {shlex.quote(remote_path)}; rm {shlex.quote(partial)}; sha256sum {shlex.quote(remote_path)}; stat -c%s {shlex.quote(remote_path)}",timeout=600)
            if remote.returncode!=0:
                print(json.dumps({"ok":False,"reason":"remote verification/rename failed","remote_stdout":remote.stdout[-300:]},indent=2)); return 5
            try:
                remote_hash=verify_remote_text(remote.stdout,args.expect_bytes,h.hexdigest())
            except ValueError as exc:
                print(json.dumps({"ok":False,"reason":str(exc),"stdout":remote.stdout[-300:]},indent=2)); return 6
            result={"ok":True,"facts_before":before,"facts_after":after,"device":args.device,"bytes":total,
                    "network_bytes":network,"compression":"gzip-zlib-level1","transfer":"ssh-gzip-d",
                    "sha256":h.hexdigest(),"remote_path":remote_path,"remote_sha256":remote_hash,
                    "lock_path":str(held.path),"lock_id":held.lock_id,"serial_lock":str(port_lock.path),
                    "at":time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            args.evidence.parent.mkdir(parents=True,exist_ok=True); args.evidence.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
            print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
        finally:
            port_lock.release()
if __name__=="__main__": sys.exit(main())
