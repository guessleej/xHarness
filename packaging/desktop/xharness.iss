; Inno Setup 6 script for the Windows installer; build.py passes AppVersion, SourceDir and IconFile.
; Installs for the person running it (no administrator needed) unless they choose all users.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F2C1B3E-7A4D-4E8F-9C21-58B0A9D3E7F4}
AppName=xHarness
AppVersion={#AppVersion}
AppVerName=xHarness {#AppVersion}
AppPublisher=xCloudinfo Corp. Limited
AppPublisherURL=https://github.com/guessleej/xHarness
AppSupportURL=https://github.com/guessleej/xHarness/issues
VersionInfoVersion={#AppVersion}
VersionInfoCompany=xCloudinfo Corp. Limited
VersionInfoProductName=xHarness
DefaultDirName={autopf}\xHarness
DefaultGroupName=xHarness
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\xHarness.exe
UninstallDisplayName=xHarness
OutputBaseFilename=xHarness-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
; An open xHarness window holds its files: ask to close it before replacing them.
CloseApplications=yes
RestartApplications=no

[Languages]
; Traditional Chinese from the Inno Setup 6.7.4 source (Files/Languages/Unofficial): the installed compiler does not ship it.
Name: "zh"; MessagesFile: "ChineseTraditional.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
zh.DesktopIcon=在桌面建立捷徑
en.DesktopIcon=Create a desktop shortcut
zh.LaunchNow=現在開啟 xHarness
en.LaunchNow=Open xHarness now
zh.KeepsData=設定、對話紀錄與記憶在 %USERPROFILE%\.xharness，解除安裝不會刪除。
en.KeepsData=Settings, conversations and memory live in %USERPROFILE%\.xharness and are kept on uninstall.

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\xHarness"; Filename: "{app}\xHarness.exe"
Name: "{autodesktop}\xHarness"; Filename: "{app}\xHarness.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\xHarness.exe"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller's runtime may leave __pycache__ folders behind.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + ExpandConstant('{cm:KeepsData}');
end;
