; Ren'Py 翻译工具 — Windows 安装包（Inno Setup 6）
; 构建: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\renpy-translator.iss
;
; 布局：
;   {app}\              应用本体（PyInstaller onedir）
;   {app}\tools\        unrpyc + python-embed（CI staging 下载）
;   {app}\.rt_home      数据目录指针（向导页选择，ssPostInstall 写入）
;   数据目录默认 {localappdata}\renpy-translator，用户可在向导或应用内修改

#define AppVersion "0.2.0"

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

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // 写入数据目录指针：应用启动时优先读取 exe 旁的 .rt_home
    ForceDirectories(DataDirPage.Values[0]);
    SaveStringToFile(ExpandConstant('{app}\.rt_home'), DataDirPage.Values[0], False);
  end;
end;
