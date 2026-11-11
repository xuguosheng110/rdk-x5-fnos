#!/usr/bin/env python3
"""One-process deployment gate: verified backup, then verified r3 write/readback.

Run as root from deploy_r3.command after macOS administrator authentication.
It never powers the board and never starts UMS.  Default is preflight; --execute
requires exact Codex acceptance, complete reviewed UMS facts, a verified backup,
and a root process.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shlex, shutil, subprocess, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from fnos.integration.hardware import macos_identity
BOARD="x5-lab-01"; LOCK_ROOT="/home/your-user/.local/state/fnos-lab"
SSH_SOCKET="/home/your-user/.cache/rdk-fnos-ssh/build-server.sock"
SSH_HOST="guosheng@192.0.2.10"
BACKUP=ROOT/"fnos/logs/uboot-diagnosis-20261106/sd-backup-evidence.json"
def run(argv,env):
    p=subprocess.run(argv,env=env,text=True)
    return p.returncode

def file_sha(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def wait_for_cache(image, status_path, expected_size, expected_sha, timeout=10800):
    """Wait for the separate cache worker; never download or kill it."""
    deadline=time.monotonic()+timeout
    last_log=0.0
    while True:
        now=time.monotonic()
        state={"image":str(image),"image_exists":image.exists(),
               "status_path":str(status_path),"status_exists":status_path.exists(),
               "elapsed_s":round(now-(deadline-timeout),1)}
        ready=False
        if image.exists() and status_path.exists():
            try:
                raw=json.loads(status_path.read_text())
                st=str(raw.get("status") or raw.get("result") or "").upper()
                sha=raw.get("image_sha256") or raw.get("sha256")
                size=raw.get("image_size") or raw.get("size")
                if st=="PASS" and sha==expected_sha and size==expected_size and image.stat().st_size==expected_size:
                    ready=True
                state.update({"status":st,"sha_match":sha==expected_sha,"size_match":size==expected_size})
            except Exception as exc:
                state["status_error"]=str(exc)
        if ready:
            print(json.dumps({"cache_ready":True,**state},ensure_ascii=False,indent=2),flush=True)
            return
        if now>=deadline:
            raise SystemExit("cache wait timeout after 3h; no physical write started")
        if now-last_log>=30 or last_log==0:
            print(json.dumps({"cache_wait":True,**state},ensure_ascii=False,indent=2),flush=True)
            last_log=now
        time.sleep(min(30,max(0.2,deadline-now)))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--device",required=True)
    ap.add_argument("--image",required=True,type=Path)
    ap.add_argument("--image-remote",required=True)
    ap.add_argument("--image-sha256",required=True)
    ap.add_argument("--image-size",required=True,type=int)
    ap.add_argument("--acceptance",required=True,type=Path)
    ap.add_argument("--reviewed-target",required=True,type=Path)
    ap.add_argument("--remote-dir",default="/data/disk1/rdk-x5-fnos-guosheng/backups/20261106-sd")
    ap.add_argument("--remote-name",default="sd-current-20261106.img")
    ap.add_argument("--cache-status",default="fnos/coordination/r3-local-cache-20261106.json")
    ap.add_argument("--cache-timeout",type=int,default=10800)
    ap.add_argument("--execute",action="store_true")
    args=ap.parse_args()
    env=dict(os.environ, FNOS_BOARD_LOCK_ROOT=LOCK_ROOT, FNOS_LAB_ENABLE_REAL_BACKENDS="1")
    acc=json.loads(args.acceptance.read_text()); reviewed=json.loads(args.reviewed_target.read_text())
    if acc.get("accepted") is not True: raise SystemExit("acceptance.accepted is not true")
    ah=acc.get("image_sha256") or (acc.get("candidate") or {}).get("sha256")
    asz=acc.get("image_size") or (acc.get("candidate") or {}).get("size")
    if ah != args.image_sha256 or asz != args.image_size: raise SystemExit("acceptance image hash/size mismatch")
    try:
        facts=macos_identity.live(args.device)
        macos_identity.compare(macos_identity.reviewed_facts(reviewed),facts)
    except Exception as exc:
        raise SystemExit(f"target not the complete reviewed live binding: {exc}")
    print(json.dumps({"preflight":True,"device":args.device,"target":facts,"image":str(args.image),
                      "cache_pending":not args.image.exists(),"execute":args.execute},ensure_ascii=False,indent=2))
    if not args.execute: return 0
    if os.geteuid()!=0: raise SystemExit("deploy_r3.py --execute must run as root via deploy_r3.command")
    # 1. Backup under the same root process; cache is NOT required yet.
    rc=run([sys.executable,str(ROOT/"fnos/integration/hardware/sd_backup.py"),
            "--device",args.device,"--expect-bytes",str(facts["size_bytes"]),
            "--expect-model",str(facts["model"]),"--remote-dir",args.remote_dir,
            "--remote-name",args.remote_name,"--evidence",str(BACKUP),"--execute"],env)
    if rc!=0: raise SystemExit(f"backup failed rc={rc}; no write attempted")
    ev=json.loads(BACKUP.read_text())
    if ev.get("ok") is not True or ev.get("bytes")!=facts["size_bytes"] or not ev.get("sha256") or not ev.get("remote_path"):
        raise SystemExit("backup evidence incomplete; no write attempted")
    # 2. Re-establish live target; it must be unchanged.
    try:
        now=macos_identity.live(args.device)
        macos_identity.compare(macos_identity.reviewed_facts(reviewed),now)
    except Exception as exc: raise SystemExit(f"target changed after backup: {exc}")
    # 3. Wait for the separate cache worker; never download/kill it.
    print(json.dumps({"stage":"cache_wait","image":str(args.image),"max_seconds":args.cache_timeout},indent=2),flush=True)
    wait_for_cache(args.image,ROOT/args.cache_status,args.image_size,args.image_sha256,args.cache_timeout)
    # 4. Verified write + exact-length readback; no CP5/boot.
    rc=run([sys.executable,str(ROOT/"fnos/integration/hardware/flash_r2.py"),
            "--device",args.device,"--image",str(args.image),"--image-remote",args.image_remote,
            "--image-sha256",args.image_sha256,"--image-size",str(args.image_size),
            "--acceptance",str(args.acceptance),"--reviewed-target",str(args.reviewed_target),
            "--backup-remote",ev["remote_path"],"--backup-sha256",ev["sha256"],
            "--backup-bytes",str(ev["bytes"]),"--card-bytes",str(facts["size_bytes"]),
            "--expect-model",str(facts["model"]),"--execute"],env)
    if rc!=0: raise SystemExit(f"flash/readback failed rc={rc}")
    print(json.dumps({"ok":True,"backup":ev["remote_path"],"backup_sha256":ev["sha256"],
                      "image_sha256":args.image_sha256,"device":args.device,
                      "next":"boot is separate reviewed serial/safe-cut controller; no CP5 action here"},indent=2))
    return 0
if __name__=="__main__": sys.exit(main())
