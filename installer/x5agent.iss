; X5Agent installer (Inno Setup 6).
;
; Produces a single x5agent-setup-<version>.exe that:
;   1. shows the license agreement (terms.txt) and requires "I accept",
;   2. lets the user pick the install folder,
;   3. extracts the compiled release + the bundled CPython 3.12 runtime,
;   4. installs the Python dependencies (bootstrap.ps1, visible console),
;   5. offers to open the console in the browser right away,
;   6. optionally registers X5Agent to start with Windows.
;
; Build: installer/build-installer.ps1 (extracts payload + runtime, runs ISCC).

#define AppName "X5Agent"
#define AppPublisher "X5Agent"
#define AppExe "runtime\python.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7F2B9C41-5D6A-4C8E-9B31-2A6E4F0C7D18}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=terms.txt
OutputDir=out
OutputBaseFilename=x5agent-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
MinVersion=10.0
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start X5Agent automatically when Windows starts"; GroupDescription: "Startup:"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
; Compiled application (no .py sources) + prompts, specs and the console UI.
Source: "payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Bundled CPython 3.12 so the compiled modules always match their ABI.
Source: "runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "bootstrap.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "start-x5agent.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "stop-x5agent.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "terms.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\start-x5agent.ps1"""; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\start-x5agent.ps1"""; WorkingDir: "{app}"; Tasks: desktopicon
; Startup entry: starts the console hidden, no browser window at login.
Name: "{userstartup}\{#AppName}"; Filename: "powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\start-x5agent.ps1"" -NoBrowser"; WorkingDir: "{app}"; Tasks: autostart

[Run]
; Dependency install runs with a visible console so pip progress is honest.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bootstrap.ps1"" -AppDir ""{app}"""; WorkingDir: "{app}"; StatusMsg: "Installing Python dependencies (several minutes)..."; Flags: waituntilterminated
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\start-x5agent.ps1"""; WorkingDir: "{app}"; Description: "Open X5Agent now (starts the console and your browser)"; Flags: postinstall nowait shellexec skipifsilent

[UninstallRun]
; Stop the console before removing files (only this folder's instance).
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\stop-x5agent.ps1"" -AppDir ""{app}"""; Flags: runhidden; RunOnceId: "StopConsole"

[UninstallDelete]
; Never touch {app}\X5Agent - that folder holds the user's data.
Type: files; Name: "{app}\installed.txt"
