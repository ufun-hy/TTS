#define MyAppName "AI Audio Client"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "AI Audio"
#define MyAppExeName "AI-Audio-Client.exe"

[Setup]
AppId={{B9C67E8A-7A60-4A88-9A90-7C3E38AF5F80}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\AI Audio Client
DefaultGroupName={#MyAppName}
OutputDir=output
OutputBaseFilename=AI-Audio-Client-Setup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest
Uninstallable=yes

[Files]
Source: "..\..\dist\AI-Audio-Client.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\config\audio-client.example.json"; DestDir: "{app}"; DestName: "config.json"; Flags: ignoreversion onlyifdoesntexist

[Dirs]
Name: "{app}\cache"
Name: "{app}\logs"

[Icons]
Name: "{autodesktop}\AI Audio Client"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\AI Audio Client"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch AI Audio Client"; Flags: nowait postinstall skipifsilent
