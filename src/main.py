"""Ren'Py游戏翻译工具 - 主界面（项目式交互）"""

import gradio as gr
import os
import sys
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from renpy_parser import RenpyParser, DialogueLine, CharacterInfo
from translator import AITranslator, TranslationConfig
from config_manager import ConfigManager, ModelConfig
from project_manager import ProjectManager, Project, ProjectInfo


class RenpyTranslatorApp:
    """Ren'Py翻译工具主应用（项目式）"""

    def __init__(self):
        # 管理器
        self.parser = RenpyParser()
        self.config_manager = ConfigManager()
        self.project_manager = ProjectManager()

        # 当前状态
        self.current_project: Optional[Project] = None
        self.translator: Optional[AITranslator] = None
        self.current_index: int = 0
        self.filter_mode: str = "all"  # all, untranslated, translated
        self.filter_character: str = ""
        self.filter_file: str = ""
        self.search_text: str = ""

        # 加载模型配置
        self.model_configs: List[ModelConfig] = self.config_manager.load_all_configs()

    # ========== 项目管理 ==========

    def get_project_list(self) -> List[ProjectInfo]:
        """获取项目列表"""
        return self.project_manager.list_projects()

    def create_project(self, name: str, game_dir: str, model_config_name: str) -> str:
        """创建新项目"""
        if not name.strip():
            return "❌ 请输入项目名称"
        if not game_dir or not os.path.isdir(game_dir):
            return "❌ 请选择有效的游戏目录"
        if self.project_manager.project_exists(name):
            return f"❌ 项目 '{name}' 已存在"

        try:
            # 创建项目
            project = self.project_manager.create_project(name, game_dir, model_config_name)

            # 解析游戏
            result = self.parser.parse_directory(game_dir, extract_rpa=True, decompile_rpyc=True)

            # 转换为可序列化的格式
            project.dialogues = [self._dialogue_to_dict(d) for d in result['dialogues']]
            project.ui_texts = [self._dialogue_to_dict(d) for d in result['ui_texts']]
            project.characters = [self._character_to_dict(c) for c in result['characters']]

            # 初始化人名词典
            for char in result['characters']:
                if char.name not in project.char_dict:
                    project.char_dict[char.name] = char.name

            # 保存项目
            self.project_manager.save_project(project)

            stats = project.get_stats()
            return f"✅ 项目创建成功！\n📁 文件数: {result['total_files']}\n💬 对话: {stats['total_dialogues']}\n🖥️ UI: {stats['total_ui_texts']}\n👤 角色: {stats['total_characters']}"

        except Exception as e:
            return f"❌ 创建失败: {str(e)}"

    def open_project(self, name: str) -> str:
        """打开项目"""
        project = self.project_manager.load_project(name)
        if not project:
            return f"❌ 项目 '{name}' 不存在"

        self.current_project = project
        self.current_index = project.last_position.get('index', 0)

        # 初始化翻译器
        if project.model_config_name:
            self._init_translator(project.model_config_name)

        stats = project.get_stats()
        return f"✅ 已打开项目: {name}\n💬 进度: {stats['translated_dialogues']}/{stats['total_dialogues']}"

    def delete_project(self, name: str) -> str:
        """删除项目"""
        if self.project_manager.delete_project(name, delete_files=True):
            if self.current_project and self.current_project.name == name:
                self.current_project = None
            return f"✅ 已删除项目: {name}"
        return "❌ 删除失败"

    def save_current_project(self) -> bool:
        """保存当前项目"""
        if not self.current_project:
            return False

        # 更新最后位置
        self.current_project.last_position = {
            "index": self.current_index,
            "file": self._get_current_file(),
            "line": self._get_current_line()
        }

        return self.project_manager.save_project(self.current_project)

    # ========== 翻译器配置 ==========

    def get_model_names(self) -> List[str]:
        """获取模型配置名称列表"""
        return [c.name for c in self.model_configs]

    def _init_translator(self, config_name: str) -> bool:
        """初始化翻译器"""
        config = self.config_manager.get_config_by_name(config_name)
        if not config:
            return False

        trans_config = TranslationConfig(
            api_base=config.api_base,
            api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            context_lines=config.context_lines
        )
        self.translator = AITranslator(trans_config)
        return True

    # ========== 对话操作 ==========

    def get_filtered_dialogues(self) -> List[Dict[str, Any]]:
        """获取筛选后的对话列表"""
        if not self.current_project:
            return []

        dialogues = self.current_project.dialogues

        # 应用筛选
        if self.filter_mode == "untranslated":
            dialogues = [d for d in dialogues if not d.get('is_translated', False)]
        elif self.filter_mode == "translated":
            dialogues = [d for d in dialogues if d.get('is_translated', False)]

        if self.filter_character:
            dialogues = [d for d in dialogues if d.get('character', '') == self.filter_character]

        if self.filter_file:
            dialogues = [d for d in dialogues if self.filter_file in d.get('file_path', '')]

        if self.search_text:
            search_lower = self.search_text.lower()
            dialogues = [d for d in dialogues
                        if search_lower in d.get('original_text', '').lower()
                        or search_lower in d.get('translated_text', '').lower()]

        return dialogues

    def get_current_dialogue(self) -> Dict[str, Any]:
        """获取当前对话"""
        filtered = self.get_filtered_dialogues()
        if not filtered or self.current_index >= len(filtered):
            return {}
        return filtered[self.current_index]

    def navigate(self, direction: str) -> str:
        """导航对话"""
        filtered = self.get_filtered_dialogues()
        if not filtered:
            return "暂无对话"

        if direction == "first":
            self.current_index = 0
        elif direction == "prev":
            self.current_index = max(0, self.current_index - 1)
        elif direction == "next":
            self.current_index = min(len(filtered) - 1, self.current_index + 1)
        elif direction == "last":
            self.current_index = len(filtered) - 1

        return self._format_dialogue_preview()

    def jump_to(self, index: int) -> str:
        """跳转到指定位置"""
        filtered = self.get_filtered_dialogues()
        if not filtered:
            return "暂无对话"

        self.current_index = max(0, min(index, len(filtered) - 1))
        return self._format_dialogue_preview()

    def translate_current(self, text: str = "", use_ai: bool = True) -> tuple:
        """翻译当前对话"""
        if not self.current_project:
            return "❌ 请先打开项目", ""

        dialogue = self.get_current_dialogue()
        if not dialogue:
            return "❌ 没有可翻译的对话", ""

        if use_ai and not self.translator:
            return "❌ 请先配置翻译器", ""

        try:
            if use_ai:
                # AI翻译
                translated = self.translator.translate_text(
                    text=dialogue['original_text'],
                    character=dialogue.get('character', ''),
                    context_before=dialogue.get('context_before', []),
                    context_after=dialogue.get('context_after', []),
                    character_dict=self.current_project.char_dict
                )
            else:
                # 手动翻译
                translated = text

            # 更新对话
            dialogue['translated_text'] = translated
            dialogue['is_translated'] = True

            # 自动保存
            self.save_current_project()

            return f"✅ 翻译完成", self._format_dialogue_preview()

        except Exception as e:
            return f"❌ 翻译失败: {str(e)}", self._format_dialogue_preview()

    def batch_translate(self, progress=gr.Progress()) -> str:
        """批量翻译"""
        if not self.current_project:
            return "❌ 请先打开项目"
        if not self.translator:
            return "❌ 请先配置翻译器"

        # 获取未翻译的对话
        to_translate = [d for d in self.current_project.dialogues if not d.get('is_translated', False)]
        if not to_translate:
            return "✅ 所有对话已翻译"

        total = len(to_translate)
        success = 0
        failed = 0

        for i, dialogue in enumerate(to_translate):
            try:
                translated = self.translator.translate_text(
                    text=dialogue['original_text'],
                    character=dialogue.get('character', ''),
                    context_before=dialogue.get('context_before', []),
                    context_after=dialogue.get('context_after', []),
                    character_dict=self.current_project.char_dict
                )
                dialogue['translated_text'] = translated
                dialogue['is_translated'] = True
                success += 1

                # 每10条保存一次
                if (i + 1) % 10 == 0:
                    self.save_current_project()

                progress((i + 1) / total, desc=f"翻译进度: {i + 1}/{total}")

            except Exception as e:
                failed += 1
                print(f"翻译失败: {e}")

        # 最终保存
        self.save_current_project()

        return f"✅ 批量翻译完成！成功: {success}, 失败: {failed}"

    # ========== 人名词典 ==========

    def get_char_dict_text(self) -> str:
        """获取人名词典文本"""
        if not self.current_project:
            return ""

        lines = []
        for en, cn in sorted(self.current_project.char_dict.items()):
            lines.append(f"{en} → {cn}")
        return "\n".join(lines)

    def save_char_dict(self, text: str) -> str:
        """保存人名词典"""
        if not self.current_project:
            return "❌ 请先打开项目"

        new_dict = {}
        for line in text.strip().split('\n'):
            if '→' in line:
                parts = line.split('→')
                en_name = parts[0].strip()
                cn_name = parts[1].strip()
                if en_name and cn_name:
                    new_dict[en_name] = cn_name

        self.current_project.char_dict = new_dict
        self.save_current_project()
        return f"✅ 已保存 {len(new_dict)} 条记录"

    # ========== 导出合并 ==========

    def export_translation(self) -> str:
        """导出翻译结果"""
        if not self.current_project:
            return "❌ 请先打开项目"

        stats = self.current_project.get_stats()

        # 生成报告
        report = f"# 翻译报告\n\n"
        report += f"项目: {self.current_project.name}\n"
        report += f"游戏: {self.current_project.game_dir}\n"
        report += f"进度: {stats['translated_dialogues']}/{stats['total_dialogues']} 对话\n\n"

        report += "## 翻译详情\n\n"
        for d in self.current_project.dialogues:
            if d.get('is_translated', False):
                report += f"### {d.get('file_path', '')}:{d.get('line_number', 0)}\n"
                report += f"- 角色: {d.get('character', '旁白')}\n"
                report += f"- 原文: {d.get('original_text', '')}\n"
                report += f"- 译文: {d.get('translated_text', '')}\n\n"

        # 保存到项目目录
        export_path = self.project_manager._get_project_dir(self.current_project.name) / "translation_report.md"
        with open(export_path, 'w', encoding='utf-8') as f:
            f.write(report)

        return f"✅ 导出成功: {export_path}"

    def merge_translation(self) -> str:
        """合并翻译到游戏文件"""
        if not self.current_project:
            return "❌ 请先打开项目"

        game_dir = self.current_project.game_dir
        merged = 0

        # 按文件分组
        file_groups = {}
        for d in self.current_project.dialogues:
            if d.get('is_translated', False):
                fp = d.get('file_path', '')
                if fp not in file_groups:
                    file_groups[fp] = []
                file_groups[fp].append(d)

        for file_path, dialogues in file_groups.items():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                # 备份
                backup_path = file_path + '.bak'
                if not os.path.exists(backup_path):
                    with open(file_path, 'r', encoding='utf-8') as f:
                        backup = f.read()
                    with open(backup_path, 'w', encoding='utf-8') as f:
                        f.write(backup)

                # 替换
                for d in dialogues:
                    line_idx = d.get('line_number', 0) - 1
                    if 0 <= line_idx < len(lines):
                        char = d.get('character', '')
                        text = d.get('translated_text', '')
                        if char:
                            lines[line_idx] = f'{char} "{text}"\n'
                        else:
                            lines[line_idx] = f'"{text}"\n'
                        merged += 1

                with open(file_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)

            except Exception as e:
                return f"❌ 合并失败 {file_path}: {str(e)}"

        return f"✅ 合并完成！已修改 {merged} 行"

    # ========== 辅助方法 ==========

    def _dialogue_to_dict(self, d: DialogueLine) -> Dict[str, Any]:
        """将 DialogueLine 转为字典"""
        return {
            "file_path": d.file_path,
            "line_number": d.line_number,
            "character": d.character,
            "original_text": d.original_text,
            "translated_text": d.translated_text,
            "is_translated": d.is_translated,
            "context_before": d.context_before or [],
            "context_after": d.context_after or []
        }

    def _character_to_dict(self, c: CharacterInfo) -> Dict[str, Any]:
        """将 CharacterInfo 转为字典"""
        return {
            "variable": c.variable,
            "name": c.name,
            "chinese_name": c.chinese_name
        }

    def _get_current_file(self) -> str:
        """获取当前对话的文件路径"""
        dialogue = self.get_current_dialogue()
        return dialogue.get('file_path', '') if dialogue else ''

    def _get_current_line(self) -> int:
        """获取当前对话的行号"""
        dialogue = self.get_current_dialogue()
        return dialogue.get('line_number', 0) if dialogue else 0

    def _format_dialogue_preview(self) -> str:
        """格式化对话预览"""
        dialogue = self.get_current_dialogue()
        if not dialogue:
            return "暂无对话"

        filtered = self.get_filtered_dialogues()
        total = len(filtered)

        preview = f"📍 位置: {dialogue.get('file_path', '')}:{dialogue.get('line_number', 0)}\n"
        preview += f"👤 角色: {dialogue.get('character', '') or '旁白'}\n"
        preview += f"📝 原文: {dialogue.get('original_text', '')}\n"
        preview += f"🔄 译文: {dialogue.get('translated_text', '') or '未翻译'}\n"
        preview += f"📊 进度: {self.current_index + 1}/{total}"

        # 上下文
        ctx_before = dialogue.get('context_before', [])
        ctx_after = dialogue.get('context_after', [])

        if ctx_before:
            preview += "\n\n--- 前文 ---\n"
            for line in ctx_before[-2:]:
                preview += f"{line}\n"

        if ctx_after:
            preview += "\n--- 后文 ---\n"
            for line in ctx_after[:2]:
                preview += f"{line}\n"

        return preview


def create_ui():
    """创建Gradio界面"""
    app = RenpyTranslatorApp()

    with gr.Blocks(title="Ren'Py游戏翻译工具") as demo:
        gr.Markdown("# 🎮 Ren'Py游戏翻译工具")
        gr.Markdown("项目式翻译管理，自动保存进度")

        with gr.Tabs():
            # ===== 项目管理 =====
            with gr.Tab("📁 项目管理"):
                gr.Markdown("### 翻译项目")

                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("#### 新建项目")
                        new_name = gr.Textbox(label="项目名称", placeholder="MyGame")
                        new_game_dir = gr.Textbox(label="游戏目录", placeholder="Z:\\game\\MyGame")
                        new_model = gr.Dropdown(
                            label="AI模型",
                            choices=app.get_model_names(),
                            value=app.get_model_names()[0] if app.get_model_names() else None
                        )
                        create_btn = gr.Button("➕ 创建项目", variant="primary")
                        create_status = gr.Textbox(label="状态", interactive=False)

                    with gr.Column(scale=2):
                        gr.Markdown("#### 已有项目")
                        project_list = gr.Dataframe(
                            headers=["项目名", "游戏目录", "进度", "更新时间"],
                            datatype=["str", "str", "str", "str"],
                            interactive=False
                        )
                        with gr.Row():
                            refresh_btn = gr.Button("🔄 刷新列表")
                            open_project_name = gr.Textbox(label="输入项目名打开", placeholder="项目名")
                            open_btn = gr.Button("📂 打开项目", variant="primary")
                            delete_btn = gr.Button("🗑️ 删除项目", variant="stop")
                        project_status = gr.Textbox(label="状态", interactive=False)

                def refresh_project_list():
                    projects = app.get_project_list()
                    data = [[p.name, p.game_dir, p.progress_text, p.updated_at[:19]] for p in projects]
                    return data

                def create_new_project(name, game_dir, model):
                    result = app.create_project(name, game_dir, model)
                    return result, refresh_project_list()

                def open_existing_project(name):
                    result = app.open_project(name)
                    return result

                def delete_existing_project(name):
                    result = app.delete_project(name)
                    return result, refresh_project_list()

                create_btn.click(
                    fn=create_new_project,
                    inputs=[new_name, new_game_dir, new_model],
                    outputs=[create_status, project_list]
                )

                refresh_btn.click(fn=refresh_project_list, outputs=[project_list])
                open_btn.click(fn=open_existing_project, inputs=[open_project_name], outputs=[project_status])
                delete_btn.click(fn=delete_existing_project, inputs=[open_project_name], outputs=[project_status, project_list])

                # 初始加载
                demo.load(fn=refresh_project_list, outputs=[project_list])

            # ===== 模型配置 =====
            with gr.Tab("⚙️ 模型配置"):
                gr.Markdown("### AI翻译接口配置")

                with gr.Row():
                    with gr.Column(scale=2):
                        config_name = gr.Textbox(label="配置名称", placeholder="GPT-4")
                        api_base = gr.Textbox(label="API地址", value="https://api.openai.com/v1")
                        api_key = gr.Textbox(label="API Key", type="password")
                        model = gr.Textbox(label="模型名称", placeholder="gpt-4")

                    with gr.Column(scale=1):
                        temperature = gr.Slider(label="Temperature", minimum=0, maximum=2, value=0.3, step=0.1)
                        max_tokens = gr.Slider(label="最大Token数", minimum=100, maximum=4000, value=1000, step=100)
                        context_lines = gr.Slider(label="上下文行数", minimum=0, maximum=10, value=3, step=1)

                with gr.Row():
                    model_selector = gr.Dropdown(label="选择配置", choices=app.get_model_names())
                    load_model_btn = gr.Button("📂 加载")
                    save_model_btn = gr.Button("💾 保存", variant="primary")
                    delete_model_btn = gr.Button("🗑️ 删除", variant="stop")

                model_status = gr.Textbox(label="状态", interactive=False)

                # 事件处理（简化）
                def save_model_config(name, api_base, api_key, model, temp, tokens, ctx):
                    from config_manager import ModelConfig
                    config = ModelConfig(name=name, api_base=api_base, api_key=api_key,
                                        model=model, temperature=temp, max_tokens=tokens,
                                        context_lines=ctx)
                    if app.config_manager.add_config(config):
                        app.model_configs = app.config_manager.load_all_configs()
                        return f"✅ 已保存: {name}", gr.update(choices=app.get_model_names())
                    return "❌ 保存失败", gr.update()

                save_model_btn.click(
                    fn=save_model_config,
                    inputs=[config_name, api_base, api_key, model, temperature, max_tokens, context_lines],
                    outputs=[model_status, model_selector]
                )

            # ===== 翻译工作台 =====
            with gr.Tab("🔤 翻译工作台"):
                gr.Markdown("### 逐句翻译")

                # 项目状态
                project_info = gr.Textbox(label="当前项目", interactive=False, lines=2)

                # 筛选和搜索
                with gr.Row():
                    search_box = gr.Textbox(label="搜索", placeholder="搜索原文/译文")
                    filter_mode = gr.Dropdown(
                        label="筛选",
                        choices=["all", "untranslated", "translated"],
                        value="all"
                    )
                    filter_char = gr.Textbox(label="角色筛选", placeholder="角色名")
                    apply_filter_btn = gr.Button("🔍 应用筛选")

                # 对话预览
                dialogue_preview = gr.Textbox(label="当前对话", interactive=False, lines=12)

                # 翻译输入
                with gr.Row():
                    translation_input = gr.Textbox(label="翻译输入（留空则AI翻译）", lines=3)

                # 操作按钮
                with gr.Row():
                    first_btn = gr.Button("⏮️ 第一句")
                    prev_btn = gr.Button("⬅️ 上一句")
                    next_btn = gr.Button("➡️ 下一句")
                    last_btn = gr.Button("⏭️ 最后")
                    jump_input = gr.Number(label="跳转到", value=0, precision=0)
                    jump_btn = gr.Button("🎯 跳转")

                with gr.Row():
                    ai_translate_btn = gr.Button("🤖 AI翻译", variant="primary")
                    manual_translate_btn = gr.Button("✏️ 手动翻译", variant="secondary")
                    save_btn = gr.Button("💾 保存项目")

                translate_status = gr.Textbox(label="状态", interactive=False)

                # 事件处理
                def update_project_info():
                    if app.current_project:
                        stats = app.current_project.get_stats()
                        return f"📁 {app.current_project.name} | 💬 {stats['translated_dialogues']}/{stats['total_dialogues']} | 🖥️ {stats['translated_ui']}/{stats['total_ui_texts']}"
                    return "❌ 未打开项目"

                def apply_filter(search, mode, char):
                    app.search_text = search
                    app.filter_mode = mode
                    app.filter_character = char
                    app.current_index = 0
                    return app._format_dialogue_preview(), update_project_info()

                def navigate_first():
                    return app.navigate("first"), update_project_info()

                def navigate_prev():
                    return app.navigate("prev"), update_project_info()

                def navigate_next():
                    return app.navigate("next"), update_project_info()

                def navigate_last():
                    return app.navigate("last"), update_project_info()

                def jump_to_index(idx):
                    app.jump_to(int(idx))
                    return app._format_dialogue_preview(), update_project_info()

                def ai_translate():
                    status, preview = app.translate_current(use_ai=True)
                    return status, preview, update_project_info()

                def manual_translate(text):
                    status, preview = app.translate_current(text=text, use_ai=False)
                    return status, preview, update_project_info()

                def save_project():
                    if app.save_current_project():
                        return "✅ 项目已保存"
                    return "❌ 保存失败"

                # 绑定事件
                apply_filter_btn.click(fn=apply_filter, inputs=[search_box, filter_mode, filter_char],
                                      outputs=[dialogue_preview, project_info])
                first_btn.click(fn=navigate_first, outputs=[dialogue_preview, project_info])
                prev_btn.click(fn=navigate_prev, outputs=[dialogue_preview, project_info])
                next_btn.click(fn=navigate_next, outputs=[dialogue_preview, project_info])
                last_btn.click(fn=navigate_last, outputs=[dialogue_preview, project_info])
                jump_btn.click(fn=jump_to_index, inputs=[jump_input], outputs=[dialogue_preview, project_info])
                ai_translate_btn.click(fn=ai_translate, outputs=[translate_status, dialogue_preview, project_info])
                manual_translate_btn.click(fn=manual_translate, inputs=[translation_input],
                                          outputs=[translate_status, dialogue_preview, project_info])
                save_btn.click(fn=save_project, outputs=[translate_status])

            # ===== 批量翻译 =====
            with gr.Tab("⚡ 批量翻译"):
                gr.Markdown("### 批量翻译所有对话")

                batch_btn = gr.Button("🚀 开始批量翻译", variant="primary", size="lg")
                batch_status = gr.Textbox(label="进度", interactive=False, lines=5)

                def batch_translate():
                    return app.batch_translate()

                batch_btn.click(fn=batch_translate, outputs=[batch_status])

            # ===== 人名词典 =====
            with gr.Tab("👤 人名词典"):
                gr.Markdown("### 管理角色翻译词典")

                dict_editor = gr.Textbox(label="人名词典（格式：英文名 → 中文名）", lines=15)
                with gr.Row():
                    load_dict_btn = gr.Button("📂 加载词典")
                    save_dict_btn = gr.Button("💾 保存词典", variant="primary")
                dict_status = gr.Textbox(label="状态", interactive=False)

                load_dict_btn.click(fn=app.get_char_dict_text, outputs=[dict_editor])
                save_dict_btn.click(fn=app.save_char_dict, inputs=[dict_editor], outputs=[dict_status])

            # ===== 导出合并 =====
            with gr.Tab("📦 导出合并"):
                gr.Markdown("### 导出翻译结果")

                with gr.Row():
                    export_btn = gr.Button("📄 导出翻译报告", variant="secondary")
                    merge_btn = gr.Button("🔄 合并到游戏", variant="primary")

                export_status = gr.Textbox(label="状态", interactive=False, lines=5)

                export_btn.click(fn=app.export_translation, outputs=[export_status])
                merge_btn.click(fn=app.merge_translation, outputs=[export_status])

        # 使用说明
        with gr.Accordion("📖 使用说明", open=False):
            gr.Markdown("""
            ## 快速开始

            1. **创建项目** - 在"项目管理"页面输入项目名、游戏目录、选择AI模型
            2. **打开项目** - 在项目列表中输入项目名，点击"打开项目"
            3. **开始翻译** - 切换到"翻译工作台"，使用AI翻译或手动翻译
            4. **自动保存** - 每次翻译后自动保存进度

            ## 功能说明

            - **项目管理** - 创建、打开、删除翻译项目
            - **模型配置** - 管理AI翻译接口配置
            - **翻译工作台** - 逐句翻译，支持搜索和筛选
            - **批量翻译** - 一次性翻译所有未翻译的对话
            - **人名词典** - 管理角色名翻译，确保一致性
            - **导出合并** - 导出翻译报告或合并到游戏文件
            """)

    return demo


if __name__ == "__main__":
    demo = create_ui()
    demo.launch(server_name="0.0.0.0", server_port=7860)
