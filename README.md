# Ren'Py 游戏翻译工具

Ren'Py 游戏汉化工具：AI 翻译 + 手动校对，支持解包/反编译、人名与角色分析、
内嵌文本提取、一键导出成品游戏。

架构：FastAPI 后端 + Vue 3（Naive UI）前端，一份前端三种形态——系统托盘、
桌面窗口（pywebview）、浏览器 WebUI。服务独立常驻后台，关窗/关浏览器不中断任务。

📖 界面介绍与操作详解见 [使用说明](docs/使用说明.md)。

## 功能特点

### 项目管理
- 游戏目录或 zip 包一键建项，自动解包 `.rpa`、反编译 `.rpyc`（仅 rpyc 游戏需要 unrpyc）
- 支持 Ren'Py 8.x 与 7.x 双引擎：按游戏 `game/` 目录自动检测引擎大版本，
  匹配对应 SDK（应用内可一键下载两个大版本的常备 SDK）
- 游戏发布新版本后「更新版本」就地升级：已有译文按原文自动继承
  （微改句子模糊匹配预填 + 复核表，失效旧译文保留可查），更新失败/取消自动恢复旧版
- 检测游戏自带中文翻译并提示处理（避免与 SDK 模板重复导入）
- 使用 Ren'Py SDK 生成标准翻译文件；项目打包导出/导入

### 人名翻译 + 人物分析（融合流程）
- 自动提取角色定义（含 DynamicCharacter、玩家可改名占位符）
- 一次 AI 调用同时翻译人名 + 分析角色特征（性格/说话风格/翻译建议等）
- 台词超长自动分段，按模型上下文窗口动态计算批量

### 字符串 / 对话翻译
- 搜索、筛选（全部/未翻译/已翻译）、分页、排序、按角色过滤
- 行内编辑即存；单条/本页/全部三种批量粒度，可随时停止
- 翻译时自动注入：术语表、人名表、角色特征、label 上下文、风格指南
- 术语表可视化管理：AI 翻译时自动积累，可手动增删改、按来源筛选
- 风格指南可手写或 AI 从台词抽样生成
- 内嵌文本提取：扫描源码中未包 `_()` 的界面/脚本文本，AI 预筛 + 人工复核
  （可查看源码上下文、单句精判、全部重判），原位标记后 SDK 重生成模板入库

### 游戏导出
- 一键导出成品：填充对话/字符串译文、写入角色名 `translate python` 覆盖块、
  注入中文字体（含 font_replacement_map 覆盖写死字体）、添加语言选择入口
- 导出后自动用对应大版本 SDK 做编译校验，失败时 AI 自动修复（译文修复/内嵌拆除）
- 自动处理 Ren'Py 的 `%` 格式化陷阱（裸 % 转义、strftime/%(name)s 保留）
- 反编译产生的 .rpy 自动移除，游戏运行原始 .rpyc

### 任务与状态
- 长任务（批量翻译/建项/导出）后台执行，进度条 + 实时日志（SSE）
- 服务重启/页面刷新自动重连回放；中断任务如实标记，翻译类重发自动跳过已完成部分
- 交互式任务可挂起等待确认（官中检测、内嵌复核），刷新后对话框自动重开
- 同类任务互斥防重（重复点击不会并发跑两个导出/批量翻译）；任务全程可取消，
  取消即时生效到 AI 调用与 SDK 子进程

## 快速开始（用户）

下载对应平台的安装包/压缩包（Release 页）：

| 平台 | 格式 |
|---|---|
| Windows | `setup-*.exe` 安装包（免管理员）或 `*-portable.zip` 免安装版 |
| macOS | `*.dmg` |
| Linux | `.deb` / `.rpm` / `.AppImage` / `.tar.gz`（x86_64 与 ARM64 双架构） |

启动后驻系统托盘，托盘菜单：打开界面 / 用浏览器打开 / 退出服务。
首次使用在「模型配置」里：下载 Ren'Py SDK（8.x/7.x 应用内一键下载，或指定已有 SDK 目录）
→ 添加 AI 模型（OpenAI 兼容接口）。

### 数据目录

- 便携版：数据存 exe 旁，整目录拷走即迁移
- 安装版：默认落平台数据目录（`%APPDATA%/renpy-translator` 等），
  安装向导与应用内「模型配置 → 数据目录」都可自定义，应用内修改自动迁移全部数据

### 反编译依赖（仅 rpyc-only 游戏）

- **安装包**：已内置 unrpyc + python-embed（Windows），开箱即用
- **便携/开发版**：放 `tools/unrpyc/`（[unrpyc v2.0.4](https://github.com/CensoredUsername/unrpyc/releases/tag/v2.0.4)
  Source code 解压重命名），冻结环境还需 `tools/python-embed/`
  （[python-3.12-embed-amd64.zip](https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip) 解压）
- 未安装时遇到 rpyc-only 游戏，建项会中断并提示安装方法

## 快速开始（开发）

```bash
git clone https://github.com/yschdxm/renpy-translator.git
cd renpy-translator
uv sync
cd web && npm ci && npm run build && cd ..
uv run python run.py
```

运行模式：

```bash
uv run python run.py              # 托盘模式（默认，自动开界面；开发时服务带日志控制台）
uv run python run.py --mode gui   # 仅桌面窗口（关窗即退，服务留后台）
uv run python run.py --mode web   # 仅打开浏览器
uv run python run.py --mode server  # 前台服务（调试用）
uv run python run.py --mode stop  # 停止后台服务
```

WebUI 地址 http://localhost:7861（环境变量 `PORT` 可改）。
前端改动后需在 `web/` 重新 `npm run build`。

运行测试：

```bash
uv run pytest          # tests/ 正式测试（导出愈合/批次重试/迁移预检等）
```

## 使用流程

1. **建项**：项目管理 → 新建项目 → 填名称/游戏目录（或上传 zip）→ 选 AI 模型。
   自动完成解包、反编译、SDK 模板生成、解析入库
2. **人名翻译**：全部翻译+分析（或逐条），人名译文与角色画像都会用于后续翻译
3. **字符串翻译**：菜单/按钮等 UI 文字；「提取内嵌文本」处理源码里没包 `_()` 的文本
4. **对话翻译**：建议先完成人名与风格指南；按角色筛选、看上下文、行内精修
5. **导出游戏**：导出页一键导出（含编译校验 + 自动修复），成品 zip 在
   `exports/<项目名>/<项目名>-translated.zip`，导出页可直接下载/在文件管理器中打开

## 打包与发布

PyInstaller 不能交叉编译，每个 OS 分别构建。GitHub Actions
（`.github/workflows/build.yml`）推 `v*` tag 或手动触发，自动产出：

```
renpy-translator-setup-<ver>-windows.exe          Windows 安装包（Inno Setup）
renpy-translator-<ver>-windows-portable.zip       Windows 免安装版
renpy-translator-<ver>-macos.dmg                  macOS（.app 封装，Apple Silicon/Intel）
renpy-translator-<ver>-linux-amd64.deb            Debian/Ubuntu（x86_64）
renpy-translator-<ver>-linux-arm64.deb            Debian/Ubuntu（ARM64）
renpy-translator-<ver>-linux-amd64.rpm            Fedora/RHEL/openSUSE（x86_64）
renpy-translator-<ver>-linux-arm64.rpm            Fedora/RHEL/openSUSE（ARM64）
renpy-translator-<ver>-linux-x86_64.AppImage      Linux 通用免安装（x86_64）
renpy-translator-<ver>-linux-aarch64.AppImage     Linux 通用免安装（ARM64）
renpy-translator-<ver>-linux-amd64.tar.gz         Linux 便携（x86_64）
renpy-translator-<ver>-linux-arm64.tar.gz         Linux 便携（ARM64）
```

版本号取 tag（推 `v0.3.0` → `0.3.0`）；手动触发的测试构建用 `dev` 占位。

本地构建（Windows）：

```bash
.venv/Scripts/pyinstaller renpy-translator.spec --noconfirm
# staging/tools/ 放入 unrpyc 与 python-embed 后：
ISCC.exe installer\renpy-translator.iss    # Inno Setup 6
```

Linux GUI 窗口需系统 GTK/WebKit：`python3-gi gir1.2-webkit2-4.1 gir1.2-appindicator3-0.1`
（deb/rpm 已声明依赖自动安装；无则托盘/浏览器模式照常用）。

## 技术架构

### 核心设计

- **前后端分离**：FastAPI REST + SSE（服务端以 db 为事件唯一事实源轮询增量推送）；
  浏览器持有 UI 状态，刷新/关页不丢任务
- **托盘常驻**：服务、窗口、浏览器均为独立进程，界面全关任务照跑
- **任务持久化**：任务与事件流落 `data/app.db`，刷新后进度对话框重连回放；
  同类任务互斥；取消统一归一化（cancelled 不误标 failed）
- **SQLite**：每项目独立 `project.db`，WAL 模式，实例级可重入锁串行化并发读写，
  翻译逐条落库
- **响亮失败**：不做静默降级——错误带完整 traceback 直达界面
- **数据根解析**：`rt_home`（指针文件 → 便携 exe 旁 → 平台数据目录）

### 项目结构

```
renpy-translator/
├── run.py                     # 统一入口（tray/gui/web/server/stop）
├── renpy-translator.spec      # PyInstaller 打包配置（含惰性加载清单注释）
├── installer/                 # Inno 脚本 / RPM spec / desktop / 图标生成
├── .github/workflows/         # 跨平台 CI 构建（冒烟含 health/deep 惰性依赖检查）
├── tests/                     # pytest 测试（导出愈合/批次重试/迁移预检）
├── server/                    # FastAPI 后端
│   ├── app.py / state.py / appdb.py / deps.py / errors.py
│   ├── jobs/                  # 任务系统（db 持久化 + 轮询 SSE + ask/answer + 取消/互斥）
│   └── api/                   # REST 路由（session/projects/texts/names/embedded/export/configs/jobs/logs/system）
├── web/                       # Vue3 + Vite + TS + Naive UI 前端
│   └── src/{api,stores,pages,components,composables}/
├── src/                       # 纯逻辑核心（与 UI 无关）
│   ├── db/                    # 项目库（base/content/character/glossary/embedded/update 六模块组合）
│   ├── database.py            # db 包门面（from db import ProjectDatabase）
│   ├── translator.py          # AI 翻译门面（批次/单条/人名/分析）
│   ├── llm_client.py          # OpenAI 兼容客户端（重试/错误分类）
│   ├── prompts.py / prompt_data.py   # prompt 模板与静态参考数据
│   ├── token_budget.py        # token 预算统一计算（批次/上下文/输出上限）
│   ├── translation_service.py # 翻译编排（分批/上下文/落库）
│   ├── renpy_parser.py / rpa_extractor.py / rpyc_decompiler.py / tl_parser.py
│   ├── ai_screener.py         # 内嵌文本 AI 预筛（agentic tool 循环）
│   ├── source_tree.py         # 源码树缓存（预筛/精判 IO 复用）
│   ├── embedded_strings.py    # 内嵌文本提取/标记
│   ├── rt_home.py             # 数据根解析
│   ├── services/              # 业务服务层（建项/更新/导出/人名/内嵌管线 + game_pipeline 公共编排）
│   └── project_manager.py / config_manager.py / logger.py / sdk_manager.py
└── data/  projects/  config/  fonts/  logs/  exports/  tools/   # 用户数据（不提交）
```

### 数据库表结构

| 表 | 说明 |
|---|------|
| `project_meta` | 项目元数据（键值对） |
| `dialogues` | 对话翻译（含 label 归属） |
| `ui_texts` | UI 字符串翻译 |
| `characters` | 角色信息（人名+画像+台词数，变量名为主键） |
| `glossary` | 术语表 |
| `embedded_candidates` | 内嵌文本候选（AI 判定持久化） |
| `data/app.db: jobs/job_events/settings` | 任务/事件流/全局设置 |

## 技术栈

- 后端：FastAPI + SSE + SQLite（Python 3.12+）
- 前端：Vue 3 + TypeScript + Naive UI + Pinia（Vite）
- 桌面：pywebview（WebView2 / WKWebView / WebKitGTK）+ pystray
- AI：OpenAI 兼容接口（DeepSeek、Claude、GPT 等）
- 打包：PyInstaller + Inno Setup / DMG / deb / rpm / AppImage

## 致谢与开源许可

本项目基于 MIT 许可发布（见 [LICENSE](LICENSE)）。感谢以下项目：

- **[Ren'Py](https://github.com/renpy/renpy)**（[官网](https://www.renpy.org/)）——
  本工具依赖其 SDK 生成翻译模板并作为导出游戏的运行引擎。Ren'Py 以 MIT 为主、
  含部分 LGPL 组件，版权归 Tom Rothamel 及贡献者所有。本工具不分发 SDK，
  由应用内引导用户从官网下载。
- **[unrpyc](https://github.com/CensoredUsername/unrpyc)**——.rpyc 反编译器，
  MIT 许可，Copyright (c) 2012-2024 Yuri K. Schlesner, CensoredUsername,
  Jackmcbarn。分发包内含其完整许可文本（tools/unrpyc/LICENSE）。

另：Windows 分发包附带 Python embeddable package（PSF 许可，许可文本见
tools/python-embed/LICENSE.txt），仅用于冻结环境下的反编译子进程。

## 注意事项

1. 请确保有游戏汉化授权
2. 翻译前建议备份游戏文件

---

## 免责声明

### 1. 工具用途

本工具（Ren'Py 游戏翻译工具）仅供**学习、研究和个人使用**。本工具的设计目的是帮助用户翻译**自己拥有合法权利**的游戏作品，或**已获得原作者明确授权**的游戏作品。

### 2. 用户责任

使用本工具的用户应：

- **遵守当地法律法规**：确保您的使用行为符合所在国家和地区的法律法规
- **尊重知识产权**：仅翻译您拥有合法权利或已获授权的游戏作品
- **获取合法授权**：在翻译任何游戏之前，请确保已获得原作者或版权持有人的明确授权
- **承担使用责任**：用户对使用本工具产生的一切后果承担全部责任

### 3. 法律合规

请注意，不同国家和地区对以下内容有不同的法律规定：

- **版权法**：翻译权属于著作权的一部分，未经授权翻译可能构成侵权
- **内容法规**：各国对数字内容的传播有不同的规定
- **数字版权**：DMCA等法律对数字内容有特殊规定
- **出口管制**：某些内容可能受出口管制法规限制

**用户有责任了解并遵守所在地区的相关法律法规。**

### 4. 开发者免责

本工具的开发者：

- **不生产、不存储、不传播**任何游戏内容
- **不鼓励、不支持任何形式的侵权行为**
- **不对用户的使用行为承担任何法律责任**
- **不对因使用本工具造成的任何直接或间接损失承担责任**
- **保留随时修改、更新或终止本工具的权利**

### 5. 版权声明

- Ren'Py 引擎版权归 [Tom Rothamel](https://www.renpy.org/) 所有
- 本工具仅为辅助翻译工具，不拥有任何游戏的版权
- 翻译后的游戏版权归原作者所有
- 用户应尊重原作者的知识产权和劳动成果

### 6. 禁止行为

严禁将本工具用于：

- 未经授权翻译受版权保护的作品
- 分发未经授权的翻译作品
- 侵犯他人知识产权
- 传播违法内容
- 任何违法行为

### 7. 免责条款

**本工具按"现状"提供，不作任何明示或暗示的保证。在任何情况下，开发者均不对以下情况承担责任：**

- 任何直接、间接、附带、特殊、惩罚性或后果性损害
- 因违反当地法律而产生的任何后果
- 因侵犯版权而产生的任何索赔

### 8. 使用即表示同意

使用本工具即表示您：

- 已阅读、理解并同意本免责声明的所有条款
- 确认您所在地区允许使用本工具
- 承诺遵守所有适用的法律法规

### 9. 法律适用

本免责声明的解释和执行应适用开发者所在地区的法律。任何争议应通过友好协商解决。

---

**最后提醒：**

- 请尊重游戏开发者的劳动成果，支持正版游戏
- 请遵守当地法律法规，尊重知识产权
- 本工具仅供学习研究，请勿用于商业用途
