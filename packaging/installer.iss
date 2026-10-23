#ifndef AppVersion
#define AppVersion "0.2.0-preview.1"
#endif
#define SingleFilename "AdPurifier-" + AppVersion + "-Setup"
#if !FileExists("..\dist\AdPurifier\models\laya-illegal-ad(advanced)\model.safetensors")
#error "Bundled Laya Advanced weights missing"
#endif
#if !FileExists("..\dist\AdPurifier\models\gemma\gemma-4-E2B_q4_0-it.gguf")
#error "Bundled Gemma E2B weights missing"
#endif
#if !FileExists("..\dist\AdPurifier\installed-assets.json")
#error "Verified installed asset manifest missing"
#endif
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
#ifdef SingleInstaller
OutputBaseFilename={#SingleFilename}
Compression=lzma2/max
SolidCompression=yes
DiskSpanning=no
#else
OutputBaseFilename=AdPurifier-0.2.0-preview.1-Setup
Compression=none
SolidCompression=no
DiskSpanning=yes
DiskSliceSize=1800000000
#endif
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
