#define AppVersion "0.2.0-preview.1"
[Setup]
AppId={{85B7AA32-7FDD-4BCB-BEF2-09B7C1CB431A}
AppName=AdPurifier
AppVersion={#AppVersion}
AppPublisher=AdPurifier
DefaultDirName={localappdata}\Programs\AdPurifier
DefaultGroupName=AdPurifier
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=AdPurifier-0.2.0-preview.1-Setup
Compression=lzma2/fast
SolidCompression=no
DiskSpanning=yes
DiskSliceSize=1800000000
WizardStyle=modern
UninstallDisplayIcon={app}\AdPurifier.exe
CloseApplications=yes
SetupLogging=yes
[Files]
Source: "..\dist\AdPurifier\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\AdPurifier"; Filename: "{app}\AdPurifier.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\AdPurifier"; Filename: "{app}\AdPurifier.exe"; WorkingDir: "{app}"
[Run]
Filename: "{app}\AdPurifier.exe"; Description: "AdPurifier 실행"; Flags: nowait postinstall skipifsilent
