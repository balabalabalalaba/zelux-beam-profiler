; Inno Setup script for the Beam Profiler installer.
; Built by .github/workflows/build-windows.yml after PyInstaller:
;   iscc /DAppVersion=0.2.0 packaging\installer.iss
; Installs per user by default (no administrator rights needed on a lab PC).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F3C2B1A-8E4D-4C7B-9A51-2D0E7B5C9F14}
AppName=Beam Profiler
AppVersion={#AppVersion}
AppVerName=Beam Profiler {#AppVersion}
AppPublisher=Beam Profiler
DefaultDirName={autopf}\Beam Profiler
DefaultGroupName=Beam Profiler
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=BeamProfiler-{#AppVersion}-setup
SetupIconFile=BeamProfiler.ico
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\BeamProfiler.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\BeamProfiler\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Beam Profiler"; Filename: "{app}\BeamProfiler.exe"
Name: "{group}\Beam Profiler (simulated beam)"; Filename: "{app}\BeamProfiler.exe"; Parameters: "--camera sim --port 8780"
Name: "{group}\Beam Profiler (troubleshooting console)"; Filename: "{app}\BeamProfilerConsole.exe"
Name: "{group}\Beam Profiler user guide"; Filename: "{app}\User guide.txt"
Name: "{autodesktop}\Beam Profiler"; Filename: "{app}\BeamProfiler.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\BeamProfiler.exe"; Description: "Start Beam Profiler now"; Flags: nowait postinstall skipifsilent
