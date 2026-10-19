"""Create a signed release descriptor from verified build artifacts, never a private key."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_runtime.online_update import file_hash, verify_release
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def archive_files(bundle):
    names = []
    for entry in bundle.infolist():
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise ValueError('Release archives cannot contain symbolic links')
        if not entry.is_dir():
            names.append(entry.filename)
    if len(names) != len(set(names)):
        raise ValueError('Release archive contains duplicate paths')
    return set(names)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, required=True, help='External Ed25519 PEM private key')
    parser.add_argument('--patch', type=Path)
    parser.add_argument('--patch-url')
    parser.add_argument('--installer', type=Path)
    parser.add_argument('--installer-url')
    parser.add_argument('--bootstrap', type=Path)
    parser.add_argument('--bootstrap-url')
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if bool(args.patch) != bool(args.patch_url):
        raise ValueError('Patch ZIP and immutable HTTPS URL must be supplied together')
    if bool(args.installer) != bool(args.installer_url):
        raise ValueError('Installer ZIP and immutable HTTPS URL must be supplied together')
    if not args.patch and not args.installer:
        raise ValueError('At least one application package is required')
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('An Ed25519 signing key is required')
    packages, manifest = [], None
    artifacts = []
    if args.patch:
        artifacts.append(('patch', args.patch, args.patch_url, 'payload/app-manifest.json'))
    if args.installer:
        artifacts.append(('installer', args.installer, args.installer_url, 'app-manifest.json'))
    for kind, archive, url, member in artifacts:
        with zipfile.ZipFile(archive) as bundle:
            current = json.loads(bundle.read(member))
            if kind == 'patch':
                allowed = {'PATCH_COMMIT.txt', 'README.md', 'Apply-Patch.ps1', 'Apply-Patch.cmd',
                           'tools/offline_patch.py'}
                expected = allowed | {'payload/' + name for name in current['files']}
                actual = archive_files(bundle)
                if actual != expected:
                    raise ValueError('Patch archive contains missing or unexpected files')
                patch_commit = bundle.read('PATCH_COMMIT.txt').decode('ascii').strip()
                if patch_commit != current['commit']:
                    raise ValueError('PATCH_COMMIT.txt does not match its app manifest')
                for name, digest in current['files'].items():
                    if hashlib.sha256(bundle.read('payload/' + name)).hexdigest() != digest:
                        raise ValueError(f'Invalid packaged file: {name}')
            else:
                allowed = {
                    'AI-Live-Studio-Windows-Test-Setup.exe', 'app-manifest.json', 'README.md',
                    'runtime/runtime-manifest.json', 'runtime/python-lock.txt',
                }
                actual = archive_files(bundle)
                binaries = {name for name in actual if re.fullmatch(
                    r'AI-Live-Studio-Windows-Test-Setup-[0-9]+\.bin', name)}
                unexpected = actual - allowed - binaries
                if unexpected or not binaries or not allowed.issubset(actual):
                    raise ValueError('Installer archive contains missing or unexpected files')
        if manifest and any(current.get(k) != manifest.get(k) for k in ('commit', 'version', 'release_sequence', 'runtime_id')):
            raise ValueError('Installer and patch must describe the same release')
        manifest = current
        packages.append(dict(kind=kind, url=url, size=archive.stat().st_size,
                             sha256=file_hash(archive), runtime_id=current['runtime_id']))
    if bool(args.bootstrap) != bool(args.bootstrap_url):
        raise ValueError('Bootstrap file and its immutable HTTPS URL must be supplied together')
    release = dict(schema=1, version=manifest['version'], sequence=manifest['release_sequence'],
                   commit=manifest['commit'], notes=args.notes.read_text(encoding='utf-8'), packages=packages)
    if args.bootstrap:
        if not args.bootstrap.is_file():
            raise ValueError('Bootstrap executable was not found')
        release['bootstrap'] = dict(version=manifest['version'], url=args.bootstrap_url,
                                    size=args.bootstrap.stat().st_size,
                                    sha256=file_hash(args.bootstrap))
    payload = json.dumps(release, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    envelope = dict(payload=base64.b64encode(payload).decode(), signature=base64.b64encode(key.sign(payload)).decode())
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    verify_release(envelope, public)
    configured = json.loads((ROOT / 'config/update-source.json').read_text())['public_key']
    if configured != public:
        raise ValueError('Signing key does not match the public key shipped in config/update-source.json')
    args.output.write_text(json.dumps(envelope, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
