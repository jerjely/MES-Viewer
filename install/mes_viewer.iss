; Inno Setup script: packs the PyInstaller folder dist\MES-Viewer into one Windows installer.
; The release workflow runs:  ISCC /DMyVersion=1.2.0 install\mes_viewer.iss
; Output: dist\MES-Viewer-<version>-Windows-setup.exe
; Installs for the current user only (no administrator rights needed) into
; %LOCALAPPDATA%\Programs\MES Viewer, with a Start menu entry and an optional desktop shortcut.

#ifndef MyVersion
  #define MyVersion "1.0.0"
#endif

[Setup]
AppId={{4E7B2D91-6A3C-4F58-9B1E-0C5D8A2F7E63}
AppName=MES Viewer
AppVersion={#MyVersion}
AppVerName=MES Viewer {#MyVersion}
AppPublisher=Gergely Szarka
AppPublisherURL=https://github.com/jerjely/MES-Viewer
AppSupportURL=https://github.com/jerjely/MES-Viewer/issues
DefaultDirName={autopf}\MES Viewer
DefaultGroupName=MES Viewer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=MES-Viewer-{#MyVersion}-Windows-setup
SetupIconFile=..\icons\mes_viewer.ico
UninstallDisplayIcon={app}\MES-Viewer.exe
UninstallDisplayName=MES Viewer {#MyVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "assocmes"; Description: "Open .mes files with MES Viewer (double-click)"; GroupDescription: "File types:"; Flags: unchecked

[Files]
Source: "..\dist\MES-Viewer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\MES Viewer"; Filename: "{app}\MES-Viewer.exe"
Name: "{group}\Uninstall MES Viewer"; Filename: "{uninstallexe}"
Name: "{autodesktop}\MES Viewer"; Filename: "{app}\MES-Viewer.exe"; Tasks: desktopicon

[Registry]
; per-user file association (HKA = HKCU for a per-user install), only if chosen
Root: HKA; Subkey: "Software\Classes\.mes\OpenWithProgids"; ValueType: string; ValueName: "MESViewer.mes"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assocmes
Root: HKA; Subkey: "Software\Classes\MESViewer.mes"; ValueType: string; ValueName: ""; ValueData: "Femtonics MES file"; Flags: uninsdeletekey; Tasks: assocmes
Root: HKA; Subkey: "Software\Classes\MESViewer.mes\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\MES-Viewer.exe,0"; Tasks: assocmes
Root: HKA; Subkey: "Software\Classes\MESViewer.mes\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\MES-Viewer.exe"" ""%1"""; Tasks: assocmes

[Run]
Filename: "{app}\MES-Viewer.exe"; Description: "Start MES Viewer now"; Flags: nowait postinstall skipifsilent
