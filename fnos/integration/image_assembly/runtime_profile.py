"""One runtime payload policy shared by the FPK and the full image.

The model inventory is the authority.  The runtime profile keeps one ONNX copy
of each native model, removes OpenVINO IR/weights and AppleDouble metadata, and
keeps only the Python packages needed by the reviewed services.  It never
disables the native face selection: the native AI manager must still dispatch
the BPU wrapper.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

MODULE = Path(__file__).resolve().parents[1] / 'acceleration' / 'module'
FULL_MODEL_INVENTORY = MODULE / 'model-inventory.json'
RUNTIME_MODEL_INVENTORY = MODULE / 'runtime-model-inventory.json'
PROFILES = ('full', 'runtime')
RUNTIME_PYTHON_EXCLUDE = frozenset({
    'pip', 'setuptools', 'skimage', 'networkx', 'imageio',
    'tifffile', 'lazy_loader', 'pybind11',
})
PYTHON_ROOT = Path('usr/lib/fnos-x5/photos-python')
MODEL_ROOT = Path('usr/share/fnos-x5/product-inputs/ai-models')
LICENSE_ROOT = Path('usr/share/fnos-x5/licenses')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(profile='runtime', path=None):
    if profile not in PROFILES:
        raise ValueError('unreviewed payload profile: ' + str(profile))
    path = Path(path) if path else (RUNTIME_MODEL_INVENTORY if profile == 'runtime'
                                    else FULL_MODEL_INVENTORY)
    data = json.loads(path.read_text())
    if not isinstance(data.get('required_files'), list) or not data['required_files']:
        raise ValueError('model inventory has no required files')
    return data


def model_allowed(path, profile='runtime', inventory_path=None):
    relative = Path(str(path))
    if profile == 'full':
        return True
    allowed = {row['path'] for row in inventory(profile, inventory_path)['required_files']}
    return str(relative) in allowed


def model_rows(rows, profile='runtime', inventory_path=None):
    """Return verified manifest rows for the profile, in inventory order."""
    data = inventory(profile, inventory_path)
    required = {row['path']: row for row in data['required_files']}
    if profile == 'runtime':
        bad = sorted(path for path in required
                     if not (path.endswith('.onnx') or path.endswith('/sp.model')))
        if bad:
            raise ValueError('runtime inventory contains non-ONNX payload: ' + ','.join(bad))
    selected = [row for row in rows
                if row['path'] in required and not Path(row['path']).name.startswith('._')]
    missing = sorted(set(required) - {row['path'] for row in selected})
    if missing:
        raise ValueError('reviewed model set is incomplete: ' + ','.join(missing))
    for row in selected:
        expected = required[row['path']]
        if row.get('sha256') != expected['sha256']:
            raise ValueError('model manifest hash differs: ' + row['path'])
        if row.get('bytes') != expected['bytes']:
            raise ValueError('model manifest size differs: ' + row['path'])
    return selected


def python_top(path):
    relative = Path(str(path))
    if not str(relative).startswith(str(PYTHON_ROOT) + '/'):
        return None
    rest = relative.relative_to(PYTHON_ROOT)
    return rest.parts[0] if rest.parts else None


def removed_distinfo(path):
    relative = Path(str(path))
    if not relative.parts or relative.parts[0] != 'python':
        return None
    for part in relative.parts[1:]:
        if part.endswith('.dist-info'):
            dist = part.split('-', 1)[0].lower().replace('-', '_')
            return part if {'scikit_image': 'skimage'}.get(dist, dist) in RUNTIME_PYTHON_EXCLUDE else None
    return None


def distinfo_license_source(path):
    relative = Path(str(path))
    distinfo = removed_distinfo(relative)
    if not distinfo or not relative.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')):
        return None
    return LICENSE_ROOT / distinfo / relative.name


def python_asset_allowed(path, profile='runtime'):
    """Filter an asset path rooted at ``python/<package>/...``."""
    if profile == 'full':
        return True
    relative = Path(str(path))
    if not relative.parts or relative.parts[0] != 'python':
        return True
    if removed_distinfo(relative):
        return False
    if len(relative.parts) > 1 and relative.parts[1] in RUNTIME_PYTHON_EXCLUDE:
        return False
    return 'tests' not in relative.parts and '__pycache__' not in relative.parts


def python_file_allowed(path, profile='runtime'):
    if profile == 'full':
        return True
    top = python_top(path)
    if top in RUNTIME_PYTHON_EXCLUDE:
        return False
    return 'tests' not in Path(str(path)).parts and '__pycache__' not in Path(str(path)).parts


def _copy_licenses(distinfo, root, relative_dest):
    target_root = Path(root) / relative_dest
    target_root.mkdir(parents=True, exist_ok=True)
    copied = []
    for path in sorted(Path(distinfo).iterdir()):
        if path.is_file() and path.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')):
            shutil.copyfile(path, target_root / path.name)
            copied.append(target_root / path.name)
    return copied


def prune_bundle(bundle_dir, profile='runtime'):
    """Prune a copied accel bundle; update bundle.json and preserve licenses."""
    if profile == 'full':
        return {'files': None, 'removed': 0}
    if profile not in PROFILES:
        raise ValueError('unreviewed payload profile: ' + str(profile))
    bundle_dir = Path(bundle_dir)
    manifest = json.loads((bundle_dir / 'bundle.json').read_text())
    rows = list(manifest.get('files', []))
    removed = set()
    for row in rows:
        if not python_file_allowed(row['path'], profile):
            removed.add(row['path'])
    python_root = bundle_dir / 'files' / PYTHON_ROOT
    for distinfo in sorted(python_root.glob('*.dist-info')):
        dist = distinfo.name.split('-', 1)[0].lower().replace('-', '_')
        alias = {'scikit_image': 'skimage'}
        if alias.get(dist, dist) not in RUNTIME_PYTHON_EXCLUDE:
            continue
        for copied in _copy_licenses(distinfo, bundle_dir / 'files', LICENSE_ROOT / distinfo.name):
            target = str(copied.relative_to(bundle_dir / 'files'))
            if not any(row['path'] == target for row in rows):
                rows.append({'path': target, 'target': target,
                             'sha256': digest(copied), 'mode': 0o644})
        for row in rows:
            if row['path'].startswith(str(PYTHON_ROOT / distinfo.name) + '/'):
                removed.add(row['path'])
    for path in removed:
        (bundle_dir / 'files' / path).unlink(missing_ok=True)
    for path in sorted(python_root.glob('*')):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    kept = []
    for row in rows:
        if row['path'] in removed:
            continue
        if row['path'].startswith(str(LICENSE_ROOT) + '/'):
            pass
        kept.append(row)
    manifest['files'] = kept
    (bundle_dir / 'bundle.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    return {'files': len(kept), 'removed': len(removed)}


def audit_root(root, profile='runtime', *, model_root=MODEL_ROOT, python_root=PYTHON_ROOT):
    """Fail closed if a staged root still contains a pruned payload."""
    root = Path(root).resolve()
    errors = []
    py = root / python_root
    if py.is_dir():
        for path in sorted(py.rglob('*')):
            if path.is_file() and not python_file_allowed(path.relative_to(root), profile):
                errors.append('pruned python file: ' + str(path.relative_to(root)))
        for distinfo in sorted(py.glob('*.dist-info')):
            dist = distinfo.name.split('-', 1)[0].lower().replace('-', '_')
            alias = {'scikit_image': 'skimage'}
            if alias.get(dist, dist) in RUNTIME_PYTHON_EXCLUDE:
                errors.append('pruned dist-info: ' + str(distinfo.relative_to(root)))
    models = root / model_root
    if models.is_dir():
        for path in sorted(models.rglob('*')):
            if not path.is_file():
                continue
            relative = Path('ai-models') / path.relative_to(models)
            if not model_allowed(relative, profile):
                errors.append('pruned model: ' + str(path.relative_to(root)))
    return {'ok': not errors, 'errors': errors[:100], 'count': len(errors)}


def prune_root(root, profile='runtime', *, model_root=MODEL_ROOT, python_root=PYTHON_ROOT):
    """Apply the same rules to an offline image root before manifest collection."""
    root = Path(root).resolve()
    if root == Path('/') or not (root / 'etc/os-release').is_file():
        raise ValueError('offline root filesystem required')
    if profile == 'full':
        return {'profile': profile, 'removed_files': 0}
    if profile not in PROFILES:
        raise ValueError('unreviewed payload profile: ' + str(profile))
    removed = []
    py = root / python_root
    if py.is_dir():
        for top in sorted(RUNTIME_PYTHON_EXCLUDE):
            path = py / top
            if path.is_dir():
                removed.extend(str(p.relative_to(root)) for p in path.rglob('*') if p.is_file())
                shutil.rmtree(path)
        for path in sorted(py.rglob('tests')):
            if path.is_dir():
                removed.extend(str(p.relative_to(root)) for p in path.rglob('*') if p.is_file())
                shutil.rmtree(path)
        for distinfo in sorted(py.glob('*.dist-info')):
            dist = distinfo.name.split('-', 1)[0].lower().replace('-', '_')
            alias = {'scikit_image': 'skimage'}
            if alias.get(dist, dist) not in RUNTIME_PYTHON_EXCLUDE:
                continue
            _copy_licenses(distinfo, root, LICENSE_ROOT / distinfo.name)
            removed.extend(str(p.relative_to(root)) for p in distinfo.rglob('*') if p.is_file())
            shutil.rmtree(distinfo)
    models = root / model_root
    if models.is_dir():
        for path in sorted(models.rglob('*')):
            if not path.is_file():
                continue
            relative = Path('ai-models') / path.relative_to(models)
            if not model_allowed(relative, profile):
                removed.append(str(path.relative_to(root)))
                path.unlink()
    return {'profile': profile, 'removed_files': len(removed)}
