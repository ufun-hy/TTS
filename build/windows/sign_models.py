"""Create a signed, immutable manifest for a prepared model directory."""
import argparse
import base64
import json
from pathlib import Path
import sys
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_runtime.model_install import _safe_model_path, verify_model_manifest
from local_runtime.online_update import file_hash
from local_runtime.update import _require_https
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def validate_default_models(root):
    required = [
        'asr/Qwen3-ASR-1.7B/config.json',
        'asr/Qwen3-ASR-1.7B/preprocessor_config.json',
        'asr/Qwen3-ASR-1.7B/tokenizer_config.json',
        'tts/CosyVoice3-2512_Q8_0.gguf', 'tts/voices.json', 'llm/Modelfile',
    ]
    missing = [name for name in required if not (root / name).is_file()]
    if not any((root / 'asr/Qwen3-ASR-1.7B').glob('*.safetensors')):
        missing.append('asr/Qwen3-ASR-1.7B/*.safetensors')
    if not any(path.is_file() for path in (root / 'llm/ollama-store/manifests').rglob('*')):
        missing.append('llm/ollama-store/manifests/*')
    if missing:
        raise ValueError('Default model package is missing required content: ' + ', '.join(missing))
    asr_config = json.loads((root / 'asr/Qwen3-ASR-1.7B/config.json').read_text(encoding='utf-8'))
    if asr_config.get('model_type') != 'qwen3_asr':
        raise ValueError('ASR config is not a Qwen3-ASR model')
    voices = json.loads((root / 'tts/voices.json').read_text(encoding='utf-8'))
    if not isinstance(voices, dict) or 'default' not in voices:
        raise ValueError('voices.json must define the default voice')
    selected = {name for name in required if (root / name).is_file()}
    for directory in ('asr/Qwen3-ASR-1.7B', 'llm/ollama-store/blobs', 'llm/ollama-store/manifests'):
        for path in (root / directory).rglob('*'):
            if path.is_symlink():
                raise ValueError(f'Symlinks are not allowed in model releases: {path}')
            if path.is_file():
                selected.add(path.relative_to(root).as_posix())
    for voice_id, entry in voices.items():
        prompt = entry.get('prompt_speech') if isinstance(entry, dict) else None
        if not isinstance(prompt, str) or Path(prompt).is_absolute():
            raise ValueError(f'Voice {voice_id} must use a relative prompt_speech path')
        _safe_model_path(prompt)
        prompt_file = root / 'tts' / prompt
        if not prompt_file.is_file():
            raise ValueError(f'Voice prompt is missing from model package: {voice_id} -> {prompt}')
        selected.add(prompt_file.relative_to(root).as_posix())
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, required=True, help='External Ed25519 PEM private key')
    parser.add_argument('--root', type=Path, required=True, help='Prepared model directory')
    parser.add_argument('--id', default='default')
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', type=int, required=True)
    parser.add_argument('--base-url', required=True, help='Immutable HTTPS directory for model files')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    _require_https(args.base_url)
    parsed = urlsplit(args.base_url)
    if parsed.query or parsed.fragment:
        raise ValueError('Model base URL cannot contain a query or fragment')
    root = args.root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Model root must be a directory')
    selected = validate_default_models(root)
    files = []
    for relative in sorted(selected):
        path = root / relative
        if path.is_symlink():
            raise ValueError(f'Symlinks are not allowed in model releases: {path}')
        if not path.is_file() or path.name == '.ai-live-studio-models.json':
            continue
        relative = path.relative_to(root).as_posix()
        encoded = quote(relative, safe='/')
        files.append({
            'path': relative,
            'url': args.base_url.rstrip('/') + '/' + encoded,
            'size': path.stat().st_size,
            'sha256': file_hash(path),
        })
    if not files:
        raise ValueError('Model directory is empty')
    release = {
        'schema': 1, 'product': 'AI Live Studio', 'id': args.id,
        'version': args.version, 'sequence': args.sequence, 'files': files,
    }
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('An Ed25519 signing key is required')
    payload = json.dumps(release, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    envelope = {
        'payload': base64.b64encode(payload).decode(),
        'signature': base64.b64encode(key.sign(payload)).decode(),
    }
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    for source in ('update-source.json', 'model-source.json'):
        configured = json.loads((ROOT / 'config' / source).read_text(encoding='utf-8'))['public_key']
        if configured != public:
            raise ValueError(f'Signing key does not match config/{source}')
    verify_model_manifest(envelope, public)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(envelope, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
