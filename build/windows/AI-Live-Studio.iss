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
Source: "stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\AI Live Studio"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\AI Live Studio"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Change Model Directory"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--choose-models"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch AI Live Studio"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopAI Live Studio Runtime"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\runtime\python\python.exe')) then
    if (not Exec(ExpandConstant('{app}\runtime\python\python.exe'),
      '"' + ExpandConstant('{app}\scripts\windows-runtime.py') + '" stop',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then
      Result := 'Cannot safely stop the existing runtime. Resolve process ownership / active services before upgrading.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Code: Integer;
begin
  if CurStep = ssPostInstall then
    if (not Exec(ExpandConstant('{app}\runtime\python\python.exe'),
      '"' + ExpandConstant('{app}\scripts\install-runtime.py') + '" "' + ExpandConstant('{app}') + '"',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then
      RaiseException('Bundled runtime extraction or verification failed. Re-run this installer before starting AI Live Studio.');
end;
