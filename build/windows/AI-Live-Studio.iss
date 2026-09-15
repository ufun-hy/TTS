#define MyAppName "AI Live Studio"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "AI Live Studio"
#define MyAppExeName "AI-Live-Studio.exe"

[Setup]
AppId={{D1BC83B6-EEA5-4F9D-8C32-BE7D9D93B4C6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\AI Live Studio
DefaultGroupName={#MyAppName}
OutputDir=output
OutputBaseFilename=AI-Live-Studio-Windows-Test-Setup
Compression=lzma2/fast
SolidCompression=yes
DiskSpanning=yes
DiskSliceSize=2000000000
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64
Uninstallable=yes
UninstallDisplayName={#MyAppName}

[Files]
Source: "..\..\dist\AI-Live-Studio.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\AI Live Studio"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\AI Live Studio"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Change Model Directory"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--choose-models"

[Run]
Filename: "{app}\runtime\python\python.exe"; Parameters: """{app}\scripts\install-runtime.py"" ""{app}"""; Description: "Installing bundled AI runtimes"; StatusMsg: "Installing bundled AI runtimes..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "Launch AI Live Studio"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopAI Live Studio Runtime"
