import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from fnos.integration.image_assembly.cloud_storage_cache import prepare, STATE

class CachePlacementTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve(); (self.root/'proc/self').mkdir(parents=True)
        (self.root/'vol1').mkdir();self.mount=self.root/'proc/self/mountinfo'
        self.mount.write_text('28 1 8:1 / /vol1 rw,relatime - btrfs /dev/sda1 rw\n')
        self.uuid='data-uuid'; self.selected='1';self.calls=[]
    def run_command(self,args,**kwargs):
        self.calls.append(args)
        return SimpleNamespace(stdout=self.selected if args[-1]=='default-volume' else self.uuid)
    def call(self):return prepare(self.root,self.run_command)
    def test_select_and_pin(self):
        env=self.call();self.assertEqual(env['RCLONE_CACHE_DIR'],str(self.root/'vol1/@sysappmeta/fnos-remote-write-cache'))
        self.selected='2';self.calls=[];self.assertEqual(self.call(),env)
        self.assertFalse(any(c[-1]=='default-volume' for c in self.calls))
    def test_missing_mount_no_cache_or_state(self):
        self.mount.write_text('')
        with self.assertRaises(RuntimeError):self.call()
        self.assertFalse((self.root/STATE).exists());self.assertFalse((self.root/'vol1/@sysappmeta').exists())
    def test_readonly_mount(self):
        self.mount.write_text('28 1 8:1 / /vol1 ro - btrfs /dev/sda1 ro\n')
        with self.assertRaises(RuntimeError):self.call()
    def test_replaced_volume(self):
        self.call();self.uuid='different-uuid'
        with self.assertRaises(ValueError):self.call()
    def test_symlink_cache(self):
        (self.root/'vol1/@sysappmeta').symlink_to(self.root/'proc')
        with self.assertRaises(ValueError):self.call()
    def test_uninitialized_default(self):
        self.selected='0'
        with self.assertRaises(RuntimeError):self.call()
    def test_missing_uuid(self):
        self.uuid=''
        with self.assertRaises(ValueError):self.call()
    def test_shared_cache_rejected(self):
        cache=self.root/'vol1/@sysappmeta/fnos-remote-write-cache';cache.mkdir(parents=True);cache.chmod(0o777)
        with self.assertRaises(ValueError):self.call()


class DisconnectedMountTest(unittest.TestCase):
    def test_only_disconnected_native_rclone_is_unmounted(self):
        import errno
        from fnos.integration.image_assembly.cloud_storage_cache import recover_disconnected_mounts
        entries='\n'.join([
          '1 0 0:1 / /vol02/1000-1-abcd rw - fuse.rclone rclone rw',
          '2 0 0:2 / /vol02/1000-1-ffff rw - fuse.rclone rclone rw',
          '3 0 0:3 / /vol1 rw - btrfs /dev/sda1 rw',
          '4 0 0:4 / /other rw - fuse.rclone rclone rw'])
        calls=[]
        def listing(path):
            if path.endswith('abcd'):raise OSError(errno.ENOTCONN,'disconnected')
            return []
        actual=recover_disconnected_mounts(entries,listing,lambda args,**kw:calls.append(args))
        self.assertEqual(actual,['/vol02/1000-1-abcd'])
        self.assertEqual(calls,[['umount','/vol02/1000-1-abcd']])

if __name__=='__main__':unittest.main()
