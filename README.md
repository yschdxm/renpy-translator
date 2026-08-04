# 🎮 Ren'Py 游戏翻译工具

Ren'Py 游戏汉化工具，支持 AI 翻译和手动翻译。FastAPI + Vue3(Naive UI) 前后端分离架构，
一份前端两种形态：桌面 GUI（pywebview）与 WebUI（浏览器/局域网）。SQLite 存储，任务持久化，断线重连。

## ✨ 功能特点

### 📂 项目管理
- 创建翻译项目，绑定游戏目录
- 自动解包 `.rpa` 文件、反编译 `.rpyc` 文件
- 使用 Ren'Py SDK 生成标准翻译文件
- 项目打包导出 / 导入
- 顶栏快速切换项目（保留在当前面板）

### 👤 人名翻译 + 人物分析（融合）
- 自动提取游戏角色名
- AI 翻译人名的同时分析角色特征（性格、说话风格、翻译建议等）
- 台词超长时自动分段处理，根据模型上下文动态调整
- 支持占位符（如 `[mc_name]`）自动识别，跳过翻译仅分析
- 人名翻译和人物分析在同一个流程中完成

### 🔤 字符串翻译
- 翻译菜单选项、按钮、提示等 UI 文字
- 支持搜索、筛选、分页
- 逐条翻译，实时刷新状态

### 💬 对话翻译
- 翻译游戏对话和旁白
- 支持搜索、筛选、按角色过滤
- 基于 label 的上下文注入（已翻译 + 未翻译混合）
- 自动注入角色特征和术语表

### 📝 术语表
- AI 翻译时自动提取游戏专有名词（地名、物品名、技能名等）
- 翻译时注入已有术语表，保持一致性
- 内置 UI 标准翻译（Save→保存，Load→读取 等）
- 大小写不敏感去重

### 📦 游戏导出
- 一键导出翻译后的游戏
- 自动配置中文字体、添加语言选择界面
- 导出目录：`projects/项目名/output/`

## 🚀 快速开始

### 安装

```bash
git clone https://github.com/yschdxm/renpy-translator.git
cd renpy-translator
uv sync
```

### 准备字体

将中文字体（如 MiSans）放入 `fonts/` 目录。

### 配置 Ren'Py SDK

1. 下载 [Ren'Py SDK](https://www.renpy.org/latest.html)
2. 解压到项目目录（如 `renpy-8.5.3-sdk`）
3. 在「模型配置」页面设置 SDK 路径

### 安装 unrpyc（反编译，仅 rpyc-only 游戏需要）

部分游戏只发布编译后的 `.rpyc` 脚本（没有 `.rpy` 源码），创建项目时需要
[unrpyc](https://github.com/CensoredUsername/unrpyc) 反编译后才能解析。
如果游戏自带 `.rpy` 源码则无需安装。

1. 下载 [unrpyc v2.0.4](https://github.com/CensoredUsername/unrpyc/releases/tag/v2.0.4) 的 Source code (zip)
2. 解压后将 `unrpyc-2.0.4` 文件夹重命名为 `unrpyc`，放到 `tools/` 目录下
3. 确认 `tools/unrpyc/unrpyc.py` 存在

未安装时遇到 rpyc-only 游戏，创建项目会中断并提示安装方法。
要求 Python 3.9+，支持 Ren'Py 8 ~ 6.18 的游戏。

### 配置 AI 模型

1. 在「模型配置」页面添加 AI 模型
2. 支持 OpenAI 兼容接口（DeepSeek、Claude 等）
3. 设置 API 地址、Key、模型名称、上下文大小

### 启动

```bash
uv run python run.py              # 托盘模式（默认，驻系统托盘，自动开界面）
uv run python run.py --mode gui   # GUI 窗口（pywebview，关窗即退，服务留后台）
uv run python run.py --mode web   # WebUI 模式（自动打开浏览器）
uv run python run.py --mode stop  # 停止后台服务
```

GUI/WebUI 访问 http://localhost:7861。

**托盘驻守**：默认模式无窗口驻系统托盘，托盘菜单：打开界面 / 用浏览器打开 /
退出服务。服务、窗口、浏览器都是独立进程——关闭任何界面都不影响进行中的任务；
只有托盘「退出服务」或 `--mode stop` 会停止服务。

首次使用或前端有改动时，先在 `web/` 目录构建前端：`npm install && npm run build`。

### 打包（PyInstaller，跨平台）

PyInstaller 不能交叉编译，需在每个 OS 上分别构建（仓库附带 GitHub Actions
自动产出三平台，见下文）。产物形式：

- **Windows**:Inno Setup 安装包（`installer/renpy-translator.iss`)——
  免管理员单用户安装（%LOCALAPPDATA%\Programs,VS Code 同款），向导中可选
  数据目录，unrpyc + python-embed 随包分发，含卸载器
- **macOS**:`.app` + DMG
- **Linux**:tar.gz(GUI 窗口需 `python3-gi gir1.2-webkit2-4.1 gir1.2-appindicator3-0.1`)

本地构建（Windows 示例，需 Inno Setup 6):

```bash
.venv/Scripts/pyinstaller renpy-translator.spec --noconfirm
# 准备 tools 物料（unrpyc + python-embed）到 staging/tools/
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\renpy-translator.iss
# 产物: dist/installer/renpy-translator-setup-*-windows.exe
```

**数据目录**：便携模式（exe 目录可写）数据存 exe 旁；安装模式自动落到平台数据目录
(%APPDATA%/renpy-translator 等），安装向导和应用内「模型配置 → 数据目录」都可
自定义，应用内修改会自动迁移全部数据。

**Ren'Py SDK**：不打进安装包（~280MB)，应用内「模型配置 → 下载 SDK」一键
下载解压（官网 zip，带进度条），手动指定路径的老方式保留。

**unrpyc**:MIT 许可，随安装包分发（tools/unrpyc);Windows 包附带
python-embed(3.12 embed-amd64，冻结环境无解释器时用于反编译子进程）;
macOS/Linux 使用系统 Python 或自放 `tools/python-embed/`。

仓库附带 GitHub Actions(`.github/workflows/build.yml`)：推 tag 或手动触发，
自动产出 Windows 安装包 / macOS DMG / Linux tar.gz，并跑健康冒烟测试。

## 📖 使用流程

```
Header: 🎮 Ren'Py Translator │ [切换项目]          进度: x/x
├── 左侧导航                    ├── 右侧内容区
│   ├── 📂 项目管理             │   └── 当前面板内容
│   ├── 👤 人名翻译（含人物分析）│
│   ├── 🔤 字符串翻译           │
│   ├── 💬 对话翻译             │
│   ├── 📦 导出游戏             │
│   └── ⚙  模型配置             │
```

### 1. 创建项目
- 点击「项目管理」→「新建项目」
- 填写项目名、游戏目录、选择 AI 模型
- 自动解包资源、生成翻译文件、预置 UI 术语表

### 2. 人名翻译 + 人物分析
- 点击「人名翻译」
- 点击「全部翻译+分析」或逐个「翻译+分析」
- AI 同时翻译人名和分析角色特征
- 台词超长时自动分段，根据模型上下文动态调整每段条数

### 3. 字符串翻译
- 点击「字符串翻译」
- 翻译菜单、按钮、提示等 UI 文字
- 翻译时自动注入术语表和上下文

### 4. 对话翻译
- 点击「对话翻译」
- 使用搜索和筛选功能
- 翻译时自动注入角色特征、术语表、label 上下文

### 5. 导出游戏
- 点击「导出游戏」→「开始导出」
- 导出目录：`projects/项目名/output/`

### 切换项目
- **顶栏下拉**：保留在当前面板，只刷新数据
- **项目管理面板**：打开项目并切换到人名翻译面板

## 🏗️ 技术架构

### 核心设计

- **前后端分离**：FastAPI 无状态 REST + SSE 推送；浏览器持有 UI 状态，刷新/开关页面不丢任务
- **后台常驻**：服务独立于界面进程运行，关闭窗口/浏览器任务照跑；重开界面自动接管进行中的任务
- **SQLite 存储**：每个项目独立 `.db` 文件，WAL 模式，单条翻译后立即写入（毫秒级）
- **任务持久化**：长任务（批量翻译/建项目/导出）与事件流落 `data/app.db`，
  刷新页面后进度对话框自动重连回放
- **交互式任务**：任务可挂起等待用户确认（官中检测、内嵌文本复核），刷新页面后对话框自动重开
- **统一翻译服务**：人名/字符串/对话翻译共用 `TranslationService`，逻辑一致
- **术语表**：AI 翻译时自动提取游戏专有名词，后续翻译自动注入

### 项目结构

```
renpy-translator/
├── run.py                     # 统一入口（gui/web/stop + server-detached）
├── server/                    # FastAPI 后端
│   ├── app.py                 # 应用工厂（CORS/SPA 挂载/异常处理/请求分级日志）
│   ├── state.py               # AppState 单例（当前项目会话）
│   ├── appdb.py               # 应用级库（settings/jobs/job_events）
│   ├── jobs/                  # 任务系统（db 持久化 + SSE + ask/answer + 取消）
│   └── api/                   # REST 路由（session/projects/texts/names/embedded/export/configs/jobs/logs/system）
├── web/                       # Vue3 + Vite + TS + Naive UI 前端
│   └── src/{api,stores,pages,components}/
├── src/                       # 纯逻辑核心（与 UI 无关）
│   ├── database.py            # SQLite 数据库层（WAL + 自动重连）
│   ├── translation_service.py # 统一翻译调度服务
│   ├── translator.py          # AI 翻译器（提示词构建 + 术语提取）
│   ├── renpy_parser.py        # Ren'Py 脚本解析器（label 归属）
│   ├── ai_screener.py         # 内嵌文本 AI 预筛（agentic tool 循环）
│   ├── embedded_strings.py    # 内嵌文本提取/标记
│   ├── rt_home.py             # 用户数据根解析（开发=仓库根，冻结=exe 目录）
│   ├── services/              # 业务服务层（无 UI 依赖）
│   │   ├── project_creation.py    # 建项目管线
│   │   ├── game_export.py         # 游戏导出
│   │   ├── name_translation.py    # 人名翻译+人物分析
│   │   └── embedded_pipeline.py   # 内嵌文本管线
│   ├── rpa_extractor.py / rpyc_decompiler.py / tl_parser.py / sdk_manager.py
│   └── project_manager.py / config_manager.py / logger.py
├── tests/                     # 后端 pytest（任务系统/内嵌复核回环等）
├── renpy-translator.spec      # PyInstaller 打包配置
├── data/app.db                # 应用级库（任务/事件/设置）
├── projects/  config/  fonts/  logs/  exports/  tools/
└── pyproject.toml
```

### 数据库表结构

| 表 | 说明 |
|---|------|
| `project_meta` | 项目元数据（键值对） |
| `dialogues` | 对话翻译（含 label 归属） |
| `ui_texts` | UI 字符串翻译 |
| `characters` | 角色信息（合并人名+分析档案+台词数） |
| `glossary` | 术语表（游戏专有名词） |
| `embedded_candidates` | 内嵌文本候选（AI 判定持久化） |
| `data/app.db: jobs/job_events/settings` | 任务/事件流/全局设置 |

## 🔧 技术栈

- **后端**: FastAPI + SSE + SQLite（Python 3.12+）
- **前端**: Vue 3 + TypeScript + Naive UI + Pinia（Vite 构建）
- **桌面壳**: pywebview（WebView2）
- **AI**: OpenAI 兼容接口
- **SDK**: Ren'Py SDK

## ⚠️ 注意事项

1. **请确保有游戏汉化授权**
2. 翻译前建议备份游戏文件

---

## ⚖️ 免责声明

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

- ❌ 未经授权翻译受版权保护的作品
- ❌ 分发未经授权的翻译作品
- ❌ 侵犯他人知识产权
- ❌ 传播违法内容
- ❌ 任何违法行为

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

- 🎮 请尊重游戏开发者的劳动成果，支持正版游戏
- ⚖️ 请遵守当地法律法规，尊重知识产权
- 💡 本工具仅供学习研究，请勿用于商业用途
