"""Build a complete local COS upload tree; never uploads or deletes files."""
import argparse
import base64
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_runtime.model_install import verify_model_manifest
from local_runtime.online_update import file_hash
from local_runtime.update import _require_https
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, required=True, help='External Ed25519 PEM private key')
    parser.add_argument('--patch', type=Path, help='Optional verified offline patch ZIP')
    parser.add_argument('--installer-dir', type=Path, required=True, help='Full installer artifact directory')
    parser.add_argument('--models-dir', type=Path, help='Optional prepared model directory for a future model release')
    parser.add_argument('--model-version', help='Required with --models-dir')
    parser.add_argument('--model-sequence', type=int, help='Required with --models-dir')
    parser.add_argument('--base-url', required=True, help='Public HTTPS COS download prefix')
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New or empty parent path for the upload tree')
    args = parser.parse_args()
    _require_https(args.base_url)
    parsed = urlsplit(args.base_url)
    if parsed.query or parsed.fragment:
        raise ValueError('Base URL cannot contain a query or fragment')
    if args.models_dir and (
            not args.model_version or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}', args.model_version)
            or args.model_sequence is None or args.model_sequence < 1):
        raise ValueError('--models-dir requires a valid --model-version and positive --model-sequence')
    if not args.models_dir and (args.model_version is not None or args.model_sequence is not None):
        raise ValueError('--model-version and --model-sequence require --models-dir')
    base = args.base_url.rstrip('/')
    update_source = read_json(ROOT / 'config/update-source.json')
    if update_source.get('manifest_url') != base + '/latest.json':
        raise ValueError('update-source.json must point to BASE_URL/latest.json before building')
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('An Ed25519 signing key is required')
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    if not public or public != update_source.get('public_key'):
        raise ValueError('Signing key does not match the application public key shipped in the installer')
    if args.models_dir:
        model_source = read_json(ROOT / 'config/model-source.json')
        if (model_source.get('manifest_url') != base + '/models/default/latest.json'
                or model_source.get('public_key') != public):
            raise ValueError('Optional model release requires a matching model source URL and public key')

    installer_dir = args.installer_dir.resolve(strict=True)
    installer_manifest = read_json(installer_dir / 'app-manifest.json')
    patch = args.patch.resolve(strict=True) if args.patch else None
    if patch:
        with zipfile.ZipFile(patch) as bundle:
            patch_manifest = json.loads(bundle.read('payload/app-manifest.json'))
        if any(installer_manifest.get(name) != patch_manifest.get(name)
               for name in ('commit', 'version', 'release_sequence', 'runtime_id')):
            raise ValueError('Patch and full installer describe different application releases')
    commit, version = installer_manifest['commit'], installer_manifest['version']
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}', version):
        raise ValueError('Application manifest has unsafe release identity fields')
    bootstrap = installer_dir / 'bootstrap/AI-Live-Studio-Bootstrap.exe'
    if not bootstrap.is_file():
        raise FileNotFoundError(bootstrap)

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    windows = output / 'windows'
    release_dir = windows / 'releases' / version / commit
    release_dir.mkdir(parents=True)
    patch_name = ''
    if patch:
        patch_name = f'{file_hash(patch)}-AI-Live-Studio-Windows-Patch.zip'
        patch_target = release_dir / patch_name
        shutil.copy2(patch, patch_target)

    installer_zip = release_dir / 'installer-staging.zip'
    required_installer_files = {
        'AI-Live-Studio-Windows-Test-Setup.exe', 'app-manifest.json', 'README.md',
        'runtime/runtime-manifest.json', 'runtime/python-lock.txt',
    }
    included_installer_files = set()
    with zipfile.ZipFile(installer_zip, 'w', compression=zipfile.ZIP_STORED) as bundle:
        for path in sorted(installer_dir.rglob('*')):
            relative = path.relative_to(installer_dir)
            if path.is_symlink():
                raise ValueError(f'Symlinks are not allowed in installer artifacts: {path}')
            name = relative.as_posix()
            if not path.is_file() or relative.parts[0] == 'bootstrap' or relative.name == 'SHA256SUMS.txt':
                continue
            if name in required_installer_files or re.fullmatch(
                    r'AI-Live-Studio-Windows-Test-Setup-[0-9]+\.bin', name):
                bundle.write(path, relative.as_posix())
                included_installer_files.add(name)
    if (not required_installer_files.issubset(included_installer_files)
            or not any(re.fullmatch(r'AI-Live-Studio-Windows-Test-Setup-[0-9]+\.bin', name)
                       for name in included_installer_files)):
        raise ValueError('Installer artifact is missing required setup files or BIN volumes')
    installer_hash = file_hash(installer_zip)
    installer_name = f'{installer_hash}-AI-Live-Studio-Windows-Test-Setup.zip'
    installer_target = release_dir / installer_name
    installer_zip.replace(installer_target)
    installer_zip = installer_target
    bootstrap_hash = file_hash(bootstrap)
    bootstrap_name = f'{bootstrap_hash}-AI-Live-Studio-Bootstrap.exe'
    bootstrap_target = release_dir / bootstrap_name
    shutil.copy2(bootstrap, bootstrap_target)
    shutil.copy2(bootstrap, windows / 'AI-Live-Studio-Bootstrap.exe')

    model_files = None
    if args.models_dir:
        model_root = args.models_dir.resolve(strict=True)
        model_base = f'{base}/models/default/{args.model_version}'
        model_latest = windows / 'models/default/latest.json'
        model_latest.parent.mkdir(parents=True)
        subprocess.run([
            sys.executable, str(ROOT / 'build/windows/sign_models.py'), '--key', str(args.key),
            '--root', str(model_root), '--version', args.model_version,
            '--sequence', str(args.model_sequence), '--base-url', model_base,
            '--output', str(model_latest),
        ], check=True)
        envelope = read_json(model_latest)
        manifest = verify_model_manifest(envelope, public)
        model_payload = json.loads(base64.b64decode(envelope['payload'], validate=True))
        model_files = model_payload['files']
        for item in model_files:
            source = model_root / item['path']
            if file_hash(source) != item['sha256']:
                raise ValueError(f'Model changed while staging: {item["path"]}')
            destination = windows / 'models/default' / args.model_version / item['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if manifest['sequence'] != args.model_sequence:
            raise ValueError('Model manifest sequence changed during staging')

    release_latest = windows / 'latest.json'
    sign_command = [
        sys.executable, str(ROOT / 'build/windows/sign_release.py'), '--key', str(args.key),
        '--installer', str(installer_zip), '--installer-url', f'{base}/releases/{version}/{commit}/{installer_name}',
        '--bootstrap', str(bootstrap), '--bootstrap-url', f'{base}/releases/{version}/{commit}/{bootstrap_name}',
        '--notes', str(args.notes), '--output', str(release_latest),
    ]
    if patch:
        sign_command += ['--patch', str(patch), '--patch-url', f'{base}/releases/{version}/{commit}/{patch_name}']
    subprocess.run(sign_command, check=True)
    print(f'COS upload tree: {windows}')
    print(f'App version: {version} ({commit})')
    print(f'Bootstrap SHA256: {bootstrap_hash}')
    print(f'Installer ZIP SHA256: {installer_hash}')
    if model_files is not None:
        print(f'Model version: {args.model_version} ({len(model_files)} files)')
    print('Upload this tree to the matching HTTPS base URL. No upload was performed.')


if __name__ == '__main__':
    main()
