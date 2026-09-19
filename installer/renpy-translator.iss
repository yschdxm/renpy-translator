; Ren'Py 翻译工具 — Windows 安装包（Inno Setup 6）
; 构建: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\renpy-translator.iss
; 版本号: CI 用 /DAppVersion=x.y.z 覆盖（取 tag），本地默认 0.2.0

#ifndef AppVersion
#define AppVersion "0.2.0"
#endif

; 布局：
;   {app}\              应用本体（PyInstaller onedir，单用户目录免管理员）
;   {app}\tools\        unrpyc + python-embed（CI staging 下载）
;   {app}\.rt_home      数据目录指针（向导页选择，ssPostInstall 写入）
;   数据目录默认 {localappdata}\renpy-translator，用户可在向导或应用内修改

[Setup]
AppName=Ren'Py 翻译工具
AppVersion={#AppVersion}
AppPublisher=yschdxm
; 单用户安装（%LOCALAPPDATA%\Programs，VS Code 同款模型）：免管理员、无 UAC
DefaultDirName={userpf}\renpy-translator
DefaultGroupName=Ren'Py 翻译工具
OutputDir=..\dist\installer
OutputBaseFilename=renpy-translator-setup-{#AppVersion}-windows
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName=Ren'Py 翻译工具
SetupIconFile=icon.ico
UninstallIconFile=icon.ico

[Files]
Source: "..\dist\renpy-translator\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\staging\tools\*"; DestDir: "{app}\tools"; Flags: recursesubdirs ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\Ren'Py 翻译工具"; Filename: "{app}\renpy-translator.exe"
Name: "{autodesktop}\Ren'Py 翻译工具"; Filename: "{app}\renpy-translator.exe"

[Run]
Filename: "{app}\renpy-translator.exe"; Description: "立即启动（驻系统托盘）"; Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
; 卸载只清程序目录；数据目录（可能在自定义位置）不动
Type: filesandordirs; Name: "{app}"

[Code]
var
  DataDirPage: TInputDirWizardPage;
  UpdateMode: Boolean;   // 原路径更新：跳过目录/数据页，保留现有 .rt_home
  AskedUpdate: Boolean;  // 上一步退回欢迎页再前进时不重问

const
  // AppId 未显式设置（默认=AppName），单用户安装的卸载键在 HKCU 下同名
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\Ren''Py 翻译工具_is1';

function GetPrevInstall(var Dir, Version: String): Boolean;
begin
  Result := RegQueryStringValue(HKCU, UninstKey, 'InstallLocation', Dir)
    and RegQueryStringValue(HKCU, UninstKey, 'DisplayVersion', Version);
end;

procedure InitializeWizard();
begin
  DataDirPage := CreateInputDirPage(wpSelectDir,
    '选择数据目录',
    '项目、配置、日志、导出等数据的存放位置',
    '默认放在用户数据目录（推荐，无需管理员权限即可写）。' + #13#10 +
    '之后也可以在应用「模型配置 → 数据目录」中修改并自动迁移。',
    False, '');
  DataDirPage.Add('数据目录：');
  DataDirPage.Values[0] := ExpandConstant('{localappdata}\renpy-translator');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  PrevDir, PrevVer: String;
begin
  Result := True;
  if (CurPageID = wpWelcome) and (not AskedUpdate) then
  begin
    AskedUpdate := True;
    // 检测到已安装版本：让用户选择原路径更新，还是全新安装另选路径
    if GetPrevInstall(PrevDir, PrevVer)
       and FileExists(AddBackslash(PrevDir) + 'renpy-translator.exe') then
    begin
      if MsgBox('检测到已安装 v' + PrevVer + #13#10 +
                '安装路径：' + PrevDir + #13#10#13#10 +
                '「是」在原路径更新（沿用现有数据目录）' + #13#10 +
                '「否」全新安装（重新选择安装路径）',
                mbConfirmation, MB_YESNO) = IDYES then
      begin
        UpdateMode := True;
        // 跳过的目录页不再显示，显式锁定目标目录
        WizardForm.DirEdit.Text := AddBackslash(PrevDir);
      end;
    end;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // 更新模式：目录与数据目录都沿用现状，两页都跳过
  Result := UpdateMode and ((PageID = wpSelectDir) or (PageID = DataDirPage.ID));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  // 关闭正在运行的实例（托盘/界面/后台服务是同一 exe 的多个进程，
  // /IM 全部结束；没有在跑时 taskkill 静默失败，无副作用）。
  // 被结束的正在执行任务会在新版首启时统一标记为 interrupted，可重发续跑
  Exec(ExpandConstant('{cmd}'),
       '/C taskkill /IM renpy-translator.exe /F >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);  // 等文件句柄释放
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not UpdateMode then
    begin
      // 写入数据目录指针：应用启动时优先读取 exe 旁的 .rt_home
      ForceDirectories(DataDirPage.Values[0]);
      SaveStringToFile(ExpandConstant('{app}\.rt_home'), DataDirPage.Values[0], False);
    end;
    // 更新模式不动 {app}\.rt_home：数据目录（可能在自定义位置）保持不变
  end;
end;
