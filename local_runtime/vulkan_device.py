"""Select an NVIDIA-only Vulkan namespace, never a machine-wide device index."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys


def nvidia_environment(environment: dict[str, str]) -> dict[str, str]:
    env = environment.copy()
    # Scope filtering to this native engine and its identity probe only.
    for name in ('GGML_VK_VISIBLE_DEVICES', 'VK_DRIVER_FILES', 'VK_ICD_FILENAMES',
                 'VK_ADD_DRIVER_FILES', 'VK_LOADER_DRIVERS_DISABLE'):
        env.pop(name, None)
    env['VK_LOADER_DRIVERS_SELECT'] = 'nv-vk64.json,nvidia*.json'
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def select_device(devices: list[dict]) -> dict:
    vulkan = [device for device in devices if device.get('name', '').startswith('Vulkan')]
    if (len(vulkan) != 1 or 'NVIDIA' not in vulkan[0].get('description', '').upper()
            or vulkan[0].get('type') != 1):  # GGML_BACKEND_DEVICE_TYPE_GPU
        raise RuntimeError('CosyVoice 需要唯一的 NVIDIA Vulkan 独显；设备过滤验证失败：' + json.dumps(vulkan, ensure_ascii=False))
    return vulkan[0]


def prepare_engine(command: list[str], environment: dict[str, str]):
    env = nvidia_environment(environment)
    engine = Path(command[0]).resolve()
    probe = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(engine.parent)],
        env=env, cwd=str(engine.parent), capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if probe.returncode:
        raise RuntimeError('NVIDIA Vulkan 设备校验失败：' + (probe.stderr or probe.stdout)[-2000:])
    try:
        selected = select_device(json.loads(probe.stdout))
    except (ValueError, TypeError) as exc:
        raise RuntimeError('NVIDIA Vulkan 设备探测返回无效数据') from exc
    argv = list(command)
    index = argv.index('--backend')
    argv[index+1] = selected['name']
    return argv, env, selected


def enumerate_devices(directory: Path):
    # Use the exact shipped GGML DLLs, with the same loader environment as the engine.
    with os.add_dll_directory(str(directory)):
        base = ctypes.CDLL(str(directory/'ggml-base.dll'))
        registry = ctypes.CDLL(str(directory/'ggml.dll'))
        registry.ggml_backend_load.argtypes = [ctypes.c_char_p]
        registry.ggml_backend_load.restype = ctypes.c_void_p
        if not registry.ggml_backend_load(os.fsencode(directory/'ggml-vulkan.dll')):
            raise RuntimeError('无法加载随包 ggml-vulkan.dll')
        registry.ggml_backend_dev_count.restype = ctypes.c_size_t
        registry.ggml_backend_dev_get.argtypes = [ctypes.c_size_t]
        registry.ggml_backend_dev_get.restype = ctypes.c_void_p
        for name in ('ggml_backend_dev_name', 'ggml_backend_dev_description'):
            getattr(base, name).argtypes = [ctypes.c_void_p]
            getattr(base, name).restype = ctypes.c_char_p
        base.ggml_backend_dev_type.argtypes = [ctypes.c_void_p]
        base.ggml_backend_dev_type.restype = ctypes.c_int
        devices = []
        for i in range(registry.ggml_backend_dev_count()):
            device = registry.ggml_backend_dev_get(i)
            devices.append({'name': base.ggml_backend_dev_name(device).decode(),
                'description': base.ggml_backend_dev_description(device).decode(),
                'type': base.ggml_backend_dev_type(device)})
        return devices


if __name__ == '__main__':
    print(json.dumps(enumerate_devices(Path(sys.argv[1])), ensure_ascii=False))
