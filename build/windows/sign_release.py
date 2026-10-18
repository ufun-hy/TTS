"""Create a signed release descriptor from verified build artifacts, never a private key."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_runtime.online_update import file_hash, verify_release
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, required=True, help='External Ed25519 PEM private key')
    parser.add_argument('--patch', type=Path, required=True)
    parser.add_argument('--patch-url', required=True)
    parser.add_argument('--installer', type=Path)
    parser.add_argument('--installer-url')
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('An Ed25519 signing key is required')
    packages, manifest = [], None
    for kind, archive, url, member in [
        ('patch', args.patch, args.patch_url, 'payload/app-manifest.json'),
        ('installer', args.installer, args.installer_url, 'app-manifest.json'),
    ]:
        if archive is None:
            continue
        if not url:
            raise ValueError('Package download URL is required')
        with zipfile.ZipFile(archive) as bundle:
            current = json.loads(bundle.read(member))
            if kind == 'patch':
                for name, digest in current['files'].items():
                    if hashlib.sha256(bundle.read('payload/' + name)).hexdigest() != digest:
                        raise ValueError(f'Invalid packaged file: {name}')
        if manifest and any(current.get(k) != manifest.get(k) for k in ('commit', 'version', 'release_sequence', 'runtime_id')):
            raise ValueError('Installer and patch must describe the same release')
        manifest = current
        packages.append(dict(kind=kind, url=url, size=archive.stat().st_size,
                             sha256=file_hash(archive), runtime_id=current['runtime_id']))
    release = dict(schema=1, version=manifest['version'], sequence=manifest['release_sequence'],
                   commit=manifest['commit'], notes=args.notes.read_text(encoding='utf-8'), packages=packages)
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
