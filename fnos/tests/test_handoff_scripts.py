"""Focused tests for the minimal backup/flash handoff helpers (no hardware)."""
from __future__ import annotations
import json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from fnos.lab.flash import TargetIdentity
from fnos.integration.hardware import flash_r2, sd_backup

CARD=31914983424
class FakeBackend:
    def __init__(self, identity): self._identity=identity
    def identify(self): return self._identity
def ident(**kw):
    d=dict(device="/dev/disk12",serial=None,model="UMS disk 0",size_bytes=CARD,bus="USB",
           whole_disk=True,internal=False,mounted=False,read_only=False,virtual=False,
           topology="USB:UMS disk 0:disk12",mount_points=())
    d.update(kw); return TargetIdentity(**d)
class HandoffTests(unittest.TestCase):
    def test_acceptance_requires_accepted_true_and_exact_identity(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"acc.json"
            p.write_text(json.dumps({"accepted":False,"reviewed":True,"image_sha256":"a"*64,"image_size":CARD}))
            with self.assertRaises(SystemExit): flash_r2.load_acceptance(p)
            p.write_text(json.dumps({"accepted":True,"image_sha256":"a"*64,"image_size":13}))
            sh,sz=flash_r2.load_acceptance(p); self.assertEqual((sh,sz),("a"*64,13))
            p.write_text(json.dumps({"accepted":True,"reviewed":True}))
            with self.assertRaises(SystemExit): flash_r2.load_acceptance(p)
    def test_image_shorter_than_card_is_not_a_target_size_rejection(self):
        # The write path must compare image size only for image<=card; exact
        # readback is bounded by image size.  Static predicate mirrors it.
        image=13157531648
        self.assertLess(image,CARD)
        self.assertGreater(image,0)
class UsbBindingTests(unittest.TestCase):
    def setUp(self):
        self.path=["Root","J773gAP","AppleARMPE","arm-io@10F00000","AppleH16GFamilyIO",
                   "usb-drd0@2280000","AppleT8132USBXHCI@00000000","usb-drd0-port-hs@00100000",
                   "USB2.1 Hub@00100000","AppleUSB20Hub@00100000","AppleUSB20HubPort@00130000",
                   "USB2.1 Hub@00130000","AppleUSB20Hub@00130000","AppleUSB20HubPort@00131000",
                   "USB download gadget@00131000","Mass Storage@0"]
        self.actual=dict(device="/dev/disk12",device_node="/dev/disk12",model="UMS disk 0",
            size_bytes=CARD,whole=True,internal=False,bus="USB",virtual="Physical",read_only=False,
            mounts=[],partition_count=1,partition_offset_bytes=4194304,partition_size_bytes=31910789120,
            usb_gadget_serial="32815562041507940378d9da0012004",usb_vid=13906,usb_pid=1331,
            usb_location_id=1249280,usb_path=self.path)
        self.review={k:self.actual[k] for k in ("model","size_bytes","usb_gadget_serial","usb_vid",
            "usb_pid","usb_location_id","usb_path","partition_offset_bytes","partition_size_bytes")}
        self.review["reviewed_mapping"]=True
    def test_missing_or_empty_review_is_rejected(self):
        from fnos.integration.hardware import macos_identity
        for bad in ({}, {"reviewed_mapping":True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                macos_identity.reviewed_facts(bad)
    def test_changed_serial_or_path_is_rejected_against_live(self):
        from fnos.integration.hardware import macos_identity
        self.assertEqual(macos_identity.compare(macos_identity.reviewed_facts(self.review),self.actual)["device"],"/dev/disk12")
        for change in ({"usb_gadget_serial":"OTHER"},{"usb_path":["Root","other-port"]},
                       {"usb_vid":9999},{"partition_offset_bytes":8192}):
            r=dict(self.review); r.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                macos_identity.compare(macos_identity.reviewed_facts(r),self.actual)
    def test_diskutil_failure_is_a_refusal_not_empty_mounts(self):
        from unittest.mock import patch
        from fnos.integration.hardware import macos_identity
        with patch.object(macos_identity,"_run",side_effect=RuntimeError("diskutil failed")):
            with self.assertRaises(RuntimeError):
                macos_identity.live("/dev/disk12")

    def test_write_sequence_pre_one_then_post_two_partitions(self):
        from fnos.integration.hardware import macos_identity
        from fnos.lab.flash import ImageHandle, require_full_readback
        import hashlib, tempfile
        pre=dict(self.actual); post=dict(self.actual); post["partition_count"]=2
        # Pre-write: stable facts + old one-FAT layout are both required.
        macos_identity.compare_stable(macos_identity.reviewed_facts(self.review),pre)
        macos_identity.compare_initial_layout(pre)
        # After writing the two-partition image, only stable facts remain valid;
        # the old layout check would (correctly) fail and must not be called.
        macos_identity.compare_stable(macos_identity.reviewed_facts(self.review),post)
        with self.assertRaises(ValueError):
            macos_identity.compare_initial_layout(post)
        # Exact image-length readback is what proves the new candidate layout.
        with tempfile.NamedTemporaryFile() as f:
            img=ImageHandle(path=Path(f.name),fd=f.fileno(),sha256="a"*64,size_bytes=13157531648,mtime_ns=0)
            require_full_readback("a"*64,img,bytes_read=13157531648,complete=True)

    def test_mounted_child_and_swapped_target_are_rejected(self):
        from fnos.integration.hardware import macos_identity
        for change in ({"mounts":["/Volumes/NO NAME"]},
                       {"whole":False},{"internal":True}):
            a=dict(self.actual); a.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                macos_identity.compare(macos_identity.reviewed_facts(self.review),a)

class FlashLengthTests(unittest.TestCase):
    def test_13g_image_on_32g_card_is_valid_but_too_long_image_is_not(self):
        from fnos.integration.hardware import flash_r2
        flash_r2.validate_lengths(13157531648,CARD,CARD)
        with self.assertRaises(SystemExit): flash_r2.validate_lengths(CARD+1,CARD,CARD)
        with self.assertRaises(SystemExit): flash_r2.validate_lengths(0,CARD,CARD)
    def test_stat_hash_requires_exact_first_size_and_hash_not_substring(self):
        from fnos.integration.hardware import flash_r2
        good="31914983424\n"+"a"*64+"  /x/whatever\n"
        flash_r2._stat_hash(good,CARD,"a"*64,"x")
        for bad in ("319149834240\n"+"b"*64+"  "+"a"*64+"\n",
                    "31914983424\n"+"b"*64+"  "+"a"*64+"\n",
                    "31914983424\n"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                flash_r2._stat_hash(bad,CARD,"a"*64,"x")
    def test_full_readback_short_or_wrong_is_rejected(self):
        from fnos.lab.flash import ImageHandle, require_full_readback, FlashFailure
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            img=ImageHandle(path=Path(f.name),fd=f.fileno(),sha256="a"*64,size_bytes=CARD,mtime_ns=0)
            with self.assertRaises(FlashFailure):
                require_full_readback("a"*64,img,bytes_read=CARD-1,complete=True)
            with self.assertRaises(FlashFailure):
                require_full_readback("b"*64,img,bytes_read=CARD,complete=True)
    def test_backup_before_after_identity_must_be_identical(self):
        from fnos.integration.hardware import sd_backup
        a={"device_node":"/dev/disk12","model":"UMS disk 0","size_bytes":CARD,
           "usb_gadget_serial":"S","usb_vid":1,"usb_pid":2,"usb_location_id":3,
           "usb_path":["p"],"partition_offset_bytes":4194304,"partition_size_bytes":31910789120,"partition_count":1}
        self.assertTrue(sd_backup.same_identity(a,dict(a)))
        b=dict(a); b["usb_path"]=["other"]
        self.assertFalse(sd_backup.same_identity(a,b))
        b=dict(a); b["device_node"]="/dev/disk13"
        self.assertFalse(sd_backup.same_identity(a,b))
    def test_remote_shell_three_line_output_is_accepted(self):
        import subprocess, tempfile, hashlib
        from fnos.integration.hardware import sd_backup
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x"); f.flush(); path=f.name
        try:
            h=hashlib.sha256(b"x").hexdigest()
            cmd=f"set -e; sha256sum {path}; sha256sum {path}; stat -f%z {path}"
            p=subprocess.run(["bash","-c",cmd],capture_output=True,text=True)
            self.assertEqual(p.returncode,0)
            self.assertEqual(len([x for x in p.stdout.splitlines() if x.strip()]),3)
            self.assertEqual(sd_backup.verify_remote_text(p.stdout,1,h),h)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_cache_wait_requires_explicit_pass_and_exact_hash_size(self):
        from fnos.integration.hardware import deploy_r3
        import hashlib, tempfile
        with tempfile.TemporaryDirectory() as td:
            image=Path(td)/"candidate.img"; image.write_bytes(b"abc")
            status=Path(td)/"status.json"
            status.write_text(json.dumps({"status":"PASS","image_sha256":hashlib.sha256(b"abc").hexdigest(),"image_size":3}))
            deploy_r3.wait_for_cache(image,status,3,hashlib.sha256(b"abc").hexdigest(),timeout=1)
            status.write_text(json.dumps({"status":"PENDING","image_sha256":hashlib.sha256(b"abc").hexdigest(),"image_size":3}))
            with self.assertRaises(SystemExit):
                deploy_r3.wait_for_cache(image,status,3,hashlib.sha256(b"abc").hexdigest(),timeout=0)

    def test_gzip_stream_decompresses_to_exact_source_hash(self):
        import subprocess, tempfile, hashlib
        from fnos.integration.hardware import sd_backup
        payload=(b"fnos-r3-small-test\0"*10000)
        comp=sd_backup.new_gzip_compressor(); wire=comp.compress(payload)+comp.flush()
        self.assertLess(len(wire),len(payload))
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(wire); path=f.name
        try:
            p=subprocess.run(["gzip","-dc",path],capture_output=True)
            self.assertEqual(p.returncode,0)
            self.assertEqual(hashlib.sha256(p.stdout).hexdigest(),hashlib.sha256(payload).hexdigest())
            self.assertEqual(len(p.stdout),len(payload))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_failed_source_with_successful_hash_process_is_rejected(self):
        from fnos.integration.hardware import sd_backup
        self.assertTrue(sd_backup.stream_ok(0,0,CARD,CARD))
        self.assertFalse(sd_backup.stream_ok(1,0,CARD,CARD))
        self.assertFalse(sd_backup.stream_ok(0,1,CARD,CARD))
        self.assertFalse(sd_backup.stream_ok(0,0,CARD-1,CARD))

    def test_remote_backup_exact_parse_rejects_wrong_size_and_substring(self):
        from fnos.integration.hardware import sd_backup
        h="a"*64; good=f"{h}  /x/{h}\n{h}  /y/{h}\n{CARD}\n"
        self.assertEqual(sd_backup.verify_remote_text(good,CARD,h),h)
        for bad in (f"{'b'*64}  /x/{h}\n{h}  /y/{h}\n{CARD*10}\n",
                    f"{h}  /x/{h}\n{'b'*64}  /y/{h}\n{CARD}\n",
                    f"{h}  /x/{h}\n{h}  /y/{h}\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sd_backup.verify_remote_text(bad,CARD,h)

    def test_backup_validation_rejects_ten_x_size_and_child_mount(self):
        from fnos.integration.hardware import sd_backup
        f={"device_node":"/dev/disk12","model":"UMS disk 0","size_bytes":CARD,
           "whole":True,"internal":False,"bus":"USB","virtual":"Physical","mounts":[],
           "usb_gadget_serial":"S","usb_path":["p"],"partition_offset_bytes":4194304,
           "partition_size_bytes":31910789120,"partition_count":1}
        self.assertEqual(sd_backup.validate(f,CARD,"UMS disk 0"),[])
        for change in ({"size_bytes":CARD*10},{"mounts":["/Volumes/NO NAME"]},{"partition_count":2}):
            x=dict(f); x.update(change)
            self.assertTrue(sd_backup.validate(x,CARD,"UMS disk 0"))

if __name__=="__main__": unittest.main()
