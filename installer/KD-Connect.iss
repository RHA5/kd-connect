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
WizardSmallImageBackColor=#111114
WizardImageStretch=yes
AppMutex=KDConnectAppMutex
CloseApplications=yes
RestartApplications=no
VersionInfoVersion=1.0.0.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion=1.0.0.0
VersionInfoDescription=Установщик {#MyAppName}
VersionInfoCopyright=Copyright (C) 2026 RHA5 (KD Metrika)

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
WelcomeLabel1=Добро пожаловать в установку KD Connect
WelcomeLabel2=Мастер установит {#MyAppName} (версия {#MyAppVersion}) на ваш компьютер.%n%nПрограмма фиксирует статистику матчей Hunt: Showdown 1896 в автоматическом режиме и работает только в связке с вашим аккаунтом на сайте kdmetrika.ru.%n%nТрекер читает лишь системные логи игры и драйвера, не внедряется в память процесса и безопасен для Easy Anti-Cheat.
WizardLicense=Соглашение и отказ от ответственности
LicenseLabel3=Перед установкой прочтите упрощенное соглашение. Для продолжения необходимо принять условия.
SelectDirDesc=Куда установить {#MyAppName}?
SelectDirLabel3=Программа будет установлена в следующую папку.
SelectDirBrowseLabel=Нажмите «Обзор...», чтобы выбрать другую папку, или «Установить», чтобы начать установку.%n%nРекомендуется папка по умолчанию: она не требует прав администратора и позволяет автообновлениям работать тихо.
FinishedHeadingLabel=Установка {#MyAppName} завершена
FinishedLabelNoIcons=Программа {#MyAppName} установлена и готова к работе.%n%nОткройте трекер и привяжите токен синхронизации из настроек на сайте kdmetrika.ru.
FinishedLabel=Программа {#MyAppName} установлена и готова к работе.%n%nОткройте трекер и привяжите токен синхронизации из настроек на сайте kdmetrika.ru.

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userprograms}\{#MyAppName}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Check: ShouldCreateDesktopShortcut

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName} после завершения"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\KDConnect"
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

procedure CloseRunningTracker;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseRunningTracker;
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    CloseRunningTracker;
  end;
end;
