"""Real exec/loader sandbox tests, compiled and run only on the build server.

The fixture codec is a hash sentinel, not a working codec. Two genuine test
shared libraries record dynamic-loader entry, and a real native executable
records argv/environment/PID, then exits or receives a signal. These tests
validate launch and fallback mechanics, never actual VPU/media functionality.
"""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.image_assembly import native_media
from fnos.integration.image_assembly.native_media import _launcher_source


PINNED_UNIT = b'''[Unit]
Description=Mediasrv Service
After=trim_main.service

[Service]
Type=simple
ExecStart=/usr/trim/bin/mediasrv -o /usr/trim/logs/mediasrv.log -a /var/run/mediasrv.socket
Environment=LD_LIBRARY_PATH=/usr/trim/lib/mediasrv/lib:/usr/trim/lib
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
'''


class NativeMediaStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'root'
        (self.root / 'etc').mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=debian\n')
        self.unit = self.root / native_media.SOURCE_UNIT
        self.unit.parent.mkdir(parents=True)
        self.unit.write_bytes(PINNED_UNIT)
        self.assertEqual(hashlib.sha256(PINNED_UNIT).hexdigest(), native_media.SOURCE_UNIT_SHA256)
        codec = self.root / native_media.NATIVE_CODEC
        codec.parent.mkdir(parents=True)
        codec.write_bytes(b'unit-test codec hash sentinel')
        self.codec_hash = hashlib.sha256(codec.read_bytes()).hexdigest()
        self.library = self.base / 'stage-test-only-arm.so'
        header = bytearray(64)
        header[:6] = b'\x7fELF\x02\x01'
        header[16:18] = (3).to_bytes(2, 'little')
        header[18:20] = (183).to_bytes(2, 'little')
        self.library.write_bytes(header)
        self.home = self.root / 'etc/fnos-x5/home-storage.env'
        self.home.parent.mkdir(parents=True)
        self.home.write_text('LD_PRELOAD=/usr/lib/fnos-x5/libhome_acl-fixture.so\nFNOS_HOME_DATA_ROOT=auto\n')
        household = self.root / 'usr/lib/fnos-x5/libhome_acl-fixture.so'
        household.parent.mkdir(parents=True)
        household.write_bytes(b'home fixture; not executed by staging tests')
        dropins = self.root / 'etc/systemd/system/mediasrv.service.d'
        dropins.mkdir()
        self.old = dropins / '80-fnos-home-storage.conf'
        self.old.write_text('[Service]\nEnvironmentFile=/etc/fnos-x5/home-storage.env\n')

    def tearDown(self):
        self.temp.cleanup()

    def stage(self):
        with patch.object(native_media, 'NATIVE_SHA256', self.codec_hash):
            return native_media.stage_native_media(self.root, self.library)

    def test_no_preload_before_abi_check_and_household_policy_unchanged(self):
        before = self.home.read_bytes(), self.old.read_bytes(), self.unit.read_bytes()
        report = self.stage()
        self.assertEqual(before, (self.home.read_bytes(), self.old.read_bytes(), self.unit.read_bytes()))
        dropin = (self.old.parent / '90-fnos-vpu.conf').read_text()
        self.assertIn('UnsetEnvironment=LD_PRELOAD ', dropin)
        self.assertNotIn('EnvironmentFile=', dropin)
        self.assertNotIn('ExecStartPre=', dropin)
        self.assertNotIn('LD_PRELOAD=', (self.home.parent / 'native-media.env').read_text())
        self.assertEqual(report['source_unit']['argv'],
            ['/usr/trim/bin/mediasrv', '-o', '/usr/trim/logs/mediasrv.log', '-a', '/var/run/mediasrv.socket'])
        self.assertFalse((self.root / 'etc/ld.so.preload').exists())
        native_media.verify_native_media(self.root, report)

    def test_unknown_unit_rejected_before_runtime_files_written(self):
        self.unit.write_bytes(PINNED_UNIT + b'\n# unknown vendor revision\n')
        with self.assertRaisesRegex(ValueError, 'unreviewed native mediasrv'):
            self.stage()
        self.assertFalse((self.root / native_media.LAUNCHER).exists())
        self.assertFalse((self.old.parent / '90-fnos-vpu.conf').exists())

    def test_other_exec_override_not_silently_replaced(self):
        (self.old.parent / '95-custom.conf').write_text('[Service]\nExecStart=\nExecStart=/other/service\n')
        with self.assertRaisesRegex(ValueError, 'ExecStart override'):
            self.stage()
        self.assertFalse((self.root / native_media.LAUNCHER).exists())

    def test_final_verification_checks_original_unit_as_well_as_payload(self):
        report = self.stage()
        self.unit.write_bytes(PINNED_UNIT.replace(b'Restart=always', b'Restart=no'))
        with self.assertRaisesRegex(ValueError, 'unreviewed native mediasrv'):
            native_media.verify_native_media(self.root, report)


SERVER_ROOT = Path('/data/disk1/rdk-x5-fnos-guosheng')
SERVER_ONLY = platform.system() == 'Linux' and Path(__file__).resolve().is_relative_to(SERVER_ROOT)


@unittest.skipUnless(SERVER_ONLY, 'actual exec/loader sandbox runs on designated build server only')
class NativeMediaLauncherExecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='media-launcher-tests-', dir=SERVER_ROOT)
        cls.base = Path(cls.temp.name)
        constructor = cls.base / 'constructor.c'
        constructor.write_text(r'''
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
__attribute__((constructor)) static void entered(void) {
    const char *path = getenv("FNOS_TEST_LOADER_LOG");
    char executable[4096];
    ssize_t n = readlink("/proc/self/exe", executable, sizeof(executable)-1);
    if (!path || n < 0) return;
    executable[n] = 0;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd >= 0) { dprintf(fd, "%s %ld %s\n", KIND, (long)getpid(), executable); close(fd); }
}
''')
        for name in ('home', 'vpu'):
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-fPIC', '-shared',
                            '-DKIND="' + name + '"', str(constructor), '-o', str(cls.base / (name + '.so'))], check=True)
        stub = cls.base / 'mediasrv-stub.c'
        stub.write_text(r'''
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *keys[] = {"LD_PRELOAD", "LD_LIBRARY_PATH", "FNOS_HOME_DATA_ROOT",
      "FNOS_X5_VPU_ENABLE", "FNOS_X5_VPU_DECODE", "FNOS_X5_VPU_TRACE", "CUSTOM_PRESERVED", NULL};
    printf("pid=%ld\n", (long)getpid());
    for (int i=0; i<argc; i++) printf("argv%d=%s\n", i, argv[i]);
    for (int i=0; keys[i]; i++) printf("%s=%s\n", keys[i], getenv(keys[i]) ? getenv(keys[i]) : "<unset>");
    fflush(stdout);
    if (getenv("FNOS_TEST_SIGNAL")) raise(SIGTERM);
    return getenv("FNOS_TEST_EXIT") ? atoi(getenv("FNOS_TEST_EXIT")) : 0;
}
''')
        cls.binary = cls.base / 'mediasrv-stub'
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(stub), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix='case-', dir=self.base))
        self.codec = self.work / 'codec-hash-sentinel'
        self.codec.write_bytes(b'fixture codec ABI bytes; not a real decoder')
        self.adapter = self.work / 'vpu.so'
        shutil.copyfile(self.base / 'vpu.so', self.adapter)
        self.home = self.work / 'home-storage.env'
        self.home.write_text('LD_PRELOAD=' + str(self.base / 'home.so') + '\nFNOS_HOME_DATA_ROOT=auto\n')
        self.command = [str(self.binary), '-o', '/tmp/log with spaces;$literal', '-a', '/tmp/mediasrv.socket']
        self.script = self.work / 'launcher.py'
        self.script.write_bytes(_launcher_source(
            native_codec=str(self.codec), native_sha256=hashlib.sha256(self.codec.read_bytes()).hexdigest(),
            adapter=str(self.adapter), adapter_sha256=hashlib.sha256(self.adapter.read_bytes()).hexdigest(),
            home_environment=str(self.home), command=self.command, enable_decode=True))
        self.trace = self.work / 'loader-events.txt'

    def invoke(self, **extra):
        env = os.environ.copy()
        # Match drop-in UnsetEnvironment. The launcher must start without preload.
        env.pop('LD_PRELOAD', None)
        env.update(LD_LIBRARY_PATH='/usr/trim/lib/mediasrv/lib:/usr/trim/lib',
                   FNOS_HOME_DATA_ROOT='auto', CUSTOM_PRESERVED='unchanged',
                   FNOS_X5_VPU_ENABLE='stale', FNOS_X5_VPU_DECODE='stale',
                   FNOS_X5_VPU_TRACE='stale', FNOS_TEST_LOADER_LOG=str(self.trace), **extra)
        process = subprocess.Popen([sys.executable, str(self.script)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate(timeout=10)
        values = dict(line.split('=', 1) for line in stdout.splitlines())
        self.assertEqual(int(values['pid']), process.pid, 'exec must preserve the systemd MainPID')
        self.assertEqual([values['argv' + str(i)] for i in range(len(self.command))], self.command)
        self.assertEqual(values['LD_LIBRARY_PATH'], env['LD_LIBRARY_PATH'])
        self.assertEqual(values['FNOS_HOME_DATA_ROOT'], 'auto')
        self.assertEqual(values['CUSTOM_PRESERVED'], 'unchanged')
        self.assertEqual(values['FNOS_X5_VPU_TRACE'], '<unset>')
        events = self.trace.read_text().splitlines()
        for event in events:
            kind, pid, executable = event.split(' ', 2)
            self.assertEqual(int(pid), process.pid)
            self.assertEqual(executable, str(self.binary), 'preload must never load into Python launcher')
        return process.returncode, values, stderr, events

    def test_matching_abi_loads_adapter_only_in_real_service(self):
        code, values, stderr, events = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(values['LD_PRELOAD'], str(self.base / 'home.so') + ':' + str(self.adapter))
        self.assertEqual(values['FNOS_X5_VPU_ENABLE'], '1')
        self.assertEqual(values['FNOS_X5_VPU_DECODE'], '1')
        self.assertEqual(sorted(e.split(' ', 1)[0] for e in events), ['home', 'vpu'])
        self.assertIn('verified adapter enabled', stderr)

    def assert_cpu_fallback(self, expected_reason, **extra):
        code, values, stderr, events = self.invoke(**extra)
        self.assertEqual(values['LD_PRELOAD'], str(self.base / 'home.so'))
        self.assertEqual(values['FNOS_X5_VPU_ENABLE'], '<unset>')
        self.assertEqual(values['FNOS_X5_VPU_DECODE'], '<unset>')
        self.assertEqual([e.split(' ', 1)[0] for e in events], ['home'])
        self.assertIn('native CPU fallback', stderr)
        self.assertIn(expected_reason, stderr)
        return code

    def test_codec_upgrade_keeps_native_service_and_exit_status(self):
        self.codec.write_bytes(b'new native codec ABI')
        self.assertEqual(self.assert_cpu_fallback('native codec ABI hash changed', FNOS_TEST_EXIT='37'), 37)

    def test_corrupt_adapter_is_never_loaded(self):
        self.adapter.write_bytes(b'broken ELF payload')
        self.assertEqual(self.assert_cpu_fallback('adapter hash changed'), 0)

    def test_missing_adapter_keeps_native_service(self):
        self.adapter.unlink()
        self.assertEqual(self.assert_cpu_fallback('runtime file unavailable'), 0)

    def test_exec_preserves_signal_lifecycle(self):
        self.codec.write_bytes(b'updated ABI')
        self.assertEqual(self.assert_cpu_fallback('native codec ABI hash changed', FNOS_TEST_SIGNAL='1'), -15)

    def test_dropin_parses_in_real_systemd(self):
        unit = self.work / 'mediasrv.service'
        unit.write_bytes(PINNED_UNIT)
        dropins = self.work / 'mediasrv.service.d'
        dropins.mkdir()
        (dropins / '90-fnos-vpu.conf').write_bytes(native_media._launcher_dropin())
        result = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
