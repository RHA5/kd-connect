; -- KD Connect Inno Setup Script --
#define MyAppName "KD Connect"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "KD Metrika"
#define MyAppURL "https://kdmetrika.ru"
#define MyAppExeName "KD-Connect.exe"

[Setup]
AppId={{9F4E2D1A-8B7C-4E12-A3D4-567890ABCDEF}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=yes
DefaultGroupName={#MyAppName}
LicenseFile=license_ru.txt
PrivilegesRequired=lowest
OutputDir=.
OutputBaseFilename=KD-Connect-Setup-v1.0.0
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
UninstallFilesDir={app}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern dark includetitlebar
WizardImageFile=wizard_large.bmp
WizardSmallImageFile=wizard_small.bmp
WizardBackColor=#09090b
WizardImageBackColor=#09090b
WizardSmallImageBackColor=#09090b
WizardImageStretch=yes
AppMutex=Local\KDConnectSingleInstanceMutex
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}.0
VersionInfoDescription=Установщик {#MyAppName}
VersionInfoCopyright=Copyright (C) 2026 RHA5 (KD Metrika)

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
WelcomeLabel1=Установка KD Connect
WelcomeLabel2=Авто-трекер матчей Hunt: Showdown 1896 (v{#MyAppVersion}) для сайта kdmetrika.ru.%n%nРаботает только с системными логами, не внедряется в игру и безопасен для Easy Anti-Cheat.
WizardLicense=Пользовательское соглашение
LicenseLabel=Условия работы трекера
LicenseLabel3=Ознакомьтесь с соглашением перед началом работы.
WizardSelectDir=Папка установки
SelectDirDesc=Выберите папку для {#MyAppName}
SelectDirLabel3=Программа установится в указанную папку.
SelectDirBrowseLabel=Рекомендуется оставить папку по умолчанию — тогда автообновления будут работать тихо без прав администратора.
FinishedHeadingLabel=Установка завершена
FinishedLabelNoIcons=Откройте программу и настройте её по пошаговой инструкции внутри.
FinishedLabel=Откройте программу и настройте её по пошаговой инструкции внутри.
ClickFinish=

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userprograms}\{#MyAppName}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Check: ShouldCreateDesktopShortcut

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  DesktopShortcutCheck: TNewCheckBox;

procedure InitializeWizard;
begin
  DesktopShortcutCheck := TNewCheckBox.Create(WizardForm);
  DesktopShortcutCheck.Parent := WizardForm.SelectDirPage;
  DesktopShortcutCheck.Left := WizardForm.DirEdit.Left;
  DesktopShortcutCheck.Top := WizardForm.DirEdit.Top + WizardForm.DirEdit.Height + ScaleY(20);
  DesktopShortcutCheck.Width := WizardForm.SelectDirPage.Width - DesktopShortcutCheck.Left;
  DesktopShortcutCheck.Height := ScaleY(22);
  DesktopShortcutCheck.Caption := 'Создать ярлык на рабочем столе';
  DesktopShortcutCheck.Checked := True;
end;

function ShouldCreateDesktopShortcut: Boolean;
begin
  Result := DesktopShortcutCheck.Checked;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // Встроенный механизм CloseApplications=yes мягко запрашивает завершение через AppMutex
  Result := '';
end;
