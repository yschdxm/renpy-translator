# Ren'Py 翻译工具 — RPM 包（Fedora/RHEL/openSUSE）
# 构建（CI）：rpmbuild -bb --define "_topdir $PWD/rpmbuild" --define "_version x.y.z" installer/rpm.spec
# 物料（%{_sourcedir} 下）：renpy-translator/（PyInstaller onedir）、
#   renpy-translator.desktop、renpy-translator.png

Name:           renpy-translator
Version:        %{_version}
Release:        1%{?dist}
Summary:        Ren'Py game translation tool (tray + WebUI)
License:        MIT
Requires:       python3-gobject gtk3 webkit2gtk4.1 libappindicator-gtk3
# PyInstaller 已打包全部依赖，关闭自动依赖扫描（防止把捆绑 so 识别成系统依赖）
AutoReqProv:    no

%description
Ren'Py 游戏翻译工具：托盘驻留 + WebUI，AI 翻译 Ren'Py 游戏。

%install
mkdir -p %{buildroot}/opt %{buildroot}/usr/bin \
         %{buildroot}/usr/share/applications \
         %{buildroot}/usr/share/icons/hicolor/256x256/apps
cp -a %{_sourcedir}/renpy-translator %{buildroot}/opt/renpy-translator
ln -s /opt/renpy-translator/renpy-translator %{buildroot}/usr/bin/renpy-translator
install -m 644 %{_sourcedir}/renpy-translator.desktop \
        %{buildroot}/usr/share/applications/
install -m 644 %{_sourcedir}/renpy-translator.png \
        %{buildroot}/usr/share/icons/hicolor/256x256/apps/

%files
/opt/renpy-translator
/usr/bin/renpy-translator
/usr/share/applications/renpy-translator.desktop
/usr/share/icons/hicolor/256x256/apps/renpy-translator.png
