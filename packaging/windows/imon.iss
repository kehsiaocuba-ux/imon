; Inno Setup script for imon (https://jrsoftware.org/isinfo.php)
; Build the executable first (packaging\windows\build.ps1 does both steps), then:
;   ISCC /DAppVersion=1.0.0 packaging\windows\imon.iss
; Produces dist\imon-<version>-setup.exe

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "imon"
#define AppPublisher "Alejandro Castaneda"
#define DistDir "..\..\dist\imon"

[Setup]
AppId={{0A7D27A4-159E-45C2-AFE2-8C9E738554B6}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
UninstallDisplayName={#AppName}
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest

[Tasks]
Name: "addtopath"; Description: "Add imon to the PATH (use 'imon' in any terminal)"; GroupDescription: "Command line:"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\imon help"; Filename: "{cmd}"; Parameters: "/k """"{app}\imon.exe"""" --help"
Name: "{group}\Uninstall imon"; Filename: "{uninstallexe}"

[Registry]
; append the install folder to the user's PATH when the task is selected
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath('{app}')

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(ExpandConstant(Param)) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;
