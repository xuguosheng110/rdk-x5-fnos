"""Grow the last partition and ext4 filesystem of a flashed X5 system SD."""

import json
from pathlib import Path
import re
import subprocess


SCRIPT = Path('usr/lib/fnos-x5/root_expand.py')
UNIT = Path('etc/systemd/system/fnos-x5-root-expand.service')
ENABLED = UNIT.parent / 'multi-user.target.wants' / UNIT.name
DONE = Path('/var/lib/fnos-x5/root-expand.done')
UNIT_TEXT = f"""[Unit]
Description=Expand the RDK X5 system SD root filesystem
After=local-fs.target
Before=fnos-product-bootstrap.service
ConditionPathExists=!{DONE}

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /{SCRIPT}
TimeoutStartSec=5min

[Install]
WantedBy=multi-user.target
"""


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def table(device):
    return json.loads(output('sfdisk', '--json', device))['partitiontable']


def target_size(layout, root, sectors):
    partitions = layout['partitions']
    if layout['label'] != 'dos' or layout.get('sectorsize') != 512 or len(partitions) != 2:
        raise ValueError('expected an X5 two-partition MBR with 512-byte sectors')
    config, system = partitions
    disk = root.removesuffix('p2')
    if (layout['device'] != disk or config['node'] != disk + 'p1'
            or config['start'] != 8192 or config['size'] != 524288
            or config['type'].lower() not in ('c', '0c')
            or system['node'] != root or system['start'] != 532480
            or system['type'] != '83' or not system.get('bootable')):
        raise ValueError('unexpected X5 system partition layout')
    end = min(sectors, 2**32) // 2048 * 2048
    if not 0 < system['size'] <= sectors - system['start']:
        raise ValueError('system partition lies outside its SD card')
    return max(system['size'], end - system['start'])


def expand():
    root, fstype = output('findmnt', '-n', '-o', 'SOURCE,FSTYPE', '/').split()
    if fstype != 'ext4' or not re.fullmatch(r'/dev/mmcblk[0-9]+p2', root):
        raise ValueError('root is not an ext4 MMC partition 2')
    if Path('/etc/device_info/boot_board').read_text().strip() != 'x5-rdk':
        raise ValueError('this system does not identify as an RDK X5')
    disk = root.removesuffix('p2')
    sysfs = Path('/sys/class/block') / Path(disk).name
    if (sysfs / 'device/type').read_text().strip() != 'SD':
        raise ValueError('root parent is not an SD card')
    sectors = int((sysfs / 'size').read_text())
    before = table(disk)
    size = target_size(before, root, sectors)
    partition_size = sysfs / Path(root).name / 'size'
    if size > before['partitions'][1]['size']:
        subprocess.run(
            ['sfdisk', '--lock=yes', '--no-reread', '--no-tell-kernel',
             '--wipe=never', '--wipe-partitions=never', '-N', '2', disk],
            input=f'size={size}\n', text=True, check=True,
        )
        before['partitions'][1]['size'] = size
        if table(disk) != before:
            raise RuntimeError('partition table did not retain the expected geometry and identity')
    if int(partition_size.read_text()) != size:
        subprocess.run(['partx', '--update', '--nr', '2', disk], check=True)
    if int(partition_size.read_text()) != size:
        raise RuntimeError('kernel has not adopted the expanded system partition')
    subprocess.run(['resize2fs', root], check=True)
    DONE.parent.mkdir(parents=True, exist_ok=True)
    marker = DONE.with_suffix('.tmp')
    marker.write_text(f'{root}: {size} sectors\n')
    marker.replace(DONE)


if __name__ == '__main__':
    expand()
