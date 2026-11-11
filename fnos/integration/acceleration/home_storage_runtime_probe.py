"""Real Linux native-unlinkat regression. Creates only a unique owned fixture."""
import ctypes, hashlib, os, pathlib, shutil, sys, tempfile
lib = ctypes.CDLL(sys.argv[1], use_errno=True)
lib.unlinkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
os.environ['FNOS_HOME_DATA_ROOT'] = '/vol*'
root = pathlib.Path(tempfile.mkdtemp(prefix='fnos-trash-validation-', dir='/vol1/1000'))
trash = root.parent / '.@#local/trash' / root.name
try:
    f = root / 'probe.txt'; f.write_bytes(b'owned recycle regression\x00\xff')
    expected = hashlib.sha256(f.read_bytes()).hexdigest()
    def recycle(p, flags=0):
        rc = lib.unlinkat(-7126, str(p).lstrip('/').encode(), flags)
        assert rc == 0, (rc, ctypes.get_errno(), str(p))
    recycle(f)
    assert not f.exists() and hashlib.sha256((trash/f.name).read_bytes()).hexdigest() == expected
    assert os.getxattr(trash/f.name, 'user.trim-trashbin-record').startswith(b'u:1000,t:')
    f.write_text('second version'); recycle(f)
    assert (trash/'probe_1.txt').read_text() == 'second version'
    assert hashlib.sha256((trash/f.name).read_bytes()).hexdigest() == expected
    folder = root/'nested'; folder.mkdir(); (folder/'keep.txt').write_text('retained')
    recycle(folder, 512)
    assert (trash/'nested/keep.txt').read_text() == 'retained'
    (root/'escape').symlink_to('/etc/os-release')
    assert lib.unlinkat(-7126, str(root/'escape').encode(), 0) == -1
    assert (root/'escape').is_symlink()
    outside_fd, outside_name = tempfile.mkstemp(prefix='fnos-trash-outside-'); os.close(outside_fd)
    outside = pathlib.Path(outside_name)
    try:
        outside.write_text('outside preserved')
        assert lib.unlinkat(-7126, str(outside).encode(), 0) == -1
        assert outside.read_text() == 'outside preserved'
    finally: outside.unlink()
    assert lib.unlinkat(-7126, str(root/'missing').encode(), 0) == -1
    assert lib.unlinkat(-7126, str(root).encode(), 0) == -1
    restored = root/'restored.txt'; (trash/'probe.txt').rename(restored)
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == expected
    assert lib.unlinkat(-7125, str(restored).lstrip('/').encode(), 0) == 0
    assert not restored.exists()
    assert lib.unlinkat(-7125, str(root/'escape').encode(), 0) == 0
    assert not (root/'escape').is_symlink() and pathlib.Path('/etc/os-release').is_file()
    assert lib.unlinkat(-7125, b'/etc/os-release', 0) == -1
    print('PASS: move, metadata, collision preservation, directory contents, symlink/outside/missing/type guards, restore hash, permanent delete, symlink unlink without target mutation')
finally:
    shutil.rmtree(root)
    if trash.exists(): shutil.rmtree(trash)
