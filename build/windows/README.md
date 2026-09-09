# Windows 构建

构建机需要 Windows、Python、PyInstaller 和 Inno Setup。运行：

```powershell
python -m pip install pyinstaller
.\build\windows\build.ps1
```

仅运行 PyInstaller 时，产物位于：

```text
dist/AI-Audio-Client.exe
```

安装 Inno Setup 后，脚本会继续生成：

```text
build/windows/output/AI-Audio-Client-Setup.exe
```

安装包包含 GUI、`config.json`、`cache/` 和 `logs/`。用户修改的 `config.json` 使用 `onlyifdoesntexist` 保留，不会被升级覆盖。
