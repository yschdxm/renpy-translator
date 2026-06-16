"""Ren'Py游戏翻译工具 - 主界面"""

import gradio as gr
import os
import sys
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from renpy_parser import RenpyParser, DialogueLine
from translator import AITranslator, TranslationConfig, CharacterDictionary


class RenpyTranslatorApp:
    """Ren'Py翻译工具主应用"""

    def __init__(self):
        self.parser = RenpyParser()
        self.translator: Optional[AITranslator] = None
        self.char_dict = CharacterDictionary()
        self.current_game_dir = ""
        self.dialogues: List[DialogueLine] = []
        self.ui_texts: List[DialogueLine] = []
        self.current_index = 0
        self.translation_config = TranslationConfig()

    def setup_translator(self, api_base: str, api_key: str,
                        model: str, temperature: float,
                        max_tokens: int, context_lines: int) -> str:
        """配置翻译器"""
        self.translation_config = TranslationConfig(
            api_base=api_base,
            api_key=api_key,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            context_lines=context_lines
        )

        if self.translator:
            self.translator.update_config(self.translation_config)
        else:
            self.translator = AITranslator(self.translation_config)

        # 测试连接
        result = self.translator.test_connection()
        if result['success']:
            return f"✅ 配置成功！模型: {result['model']}"
        else:
            return f"❌ 配置失败: {result['error']}"

    def load_game(self, game_dir: str,
                  include_ui: bool = False) -> tuple:
        """加载游戏目录"""
        if not game_dir or not os.path.isdir(game_dir):
            return "❌ 请选择有效的游戏目录", "", ""

        self.current_game_dir = game_dir

        # 解析游戏文件
        result = self.parser.parse_directory(game_dir, include_ui=include_ui)

        self.dialogues = result['dialogues']
        self.ui_texts = result['ui_texts']
        self.current_index = 0

        # 更新人名词典
        self.char_dict.update_from_characters(result['characters'])

        # 生成统计信息
        stats = f"✅ 加载完成！\n"
        stats += f"📁 扫描文件数: {result['total_files']}\n"
        stats += f"💬 对话行数: {len(self.dialogues)}\n"
        stats += f"🖥️ UI文字数: {len(self.ui_texts)}\n"
        stats += f"👤 角色数: {len(result['characters'])}"

        # 角色列表
        char_list = "角色列表：\n"
        for char in result['characters']:
            cn_name = self.char_dict.get_dict().get(char.name, "未翻译")
            char_list += f"  {char.variable}: {char.name} → {cn_name}\n"

        # 当前对话预览
        preview = self._get_current_dialogue_preview()

        return stats, char_list, preview

    def _get_current_dialogue_preview(self) -> str:
        """获取当前对话预览"""
        if not self.dialogues:
            "暂无对话"

        if self.current_index >= len(self.dialogues):
            self.current_index = len(self.dialogues) - 1

        dialogue = self.dialogues[self.current_index]

        preview = f"📍 位置: {dialogue.file_path}:{dialogue.line_number}\n"
        preview += f"👤 角色: {dialogue.character or '旁白'}\n"
        preview += f"📝 原文: {dialogue.original_text}\n"
        preview += f"🔄 译文: {dialogue.translated_text or '未翻译'}\n"
        preview += f"📊 进度: {self.current_index + 1}/{len(self.dialogues)}"

        # 显示上下文
        if dialogue.context_before:
            preview += "\n\n--- 前文 ---\n"
            for line in dialogue.context_before[-2:]:
                preview += f"{line}\n"

        if dialogue.context_after:
            preview += "\n--- 后文 ---\n"
            for line in dialogue.context_after[:2]:
                preview += f"{line}\n"

        return preview

    def navigate_dialogue(self, direction: str) -> tuple:
        """导航对话"""
        if not self.dialogues:
            return "暂无对话", "暂无对话"

        if direction == "prev":
            self.current_index = max(0, self.current_index - 1)
        elif direction == "next":
            self.current_index = min(len(self.dialogues) - 1, self.current_index + 1)
        elif direction == "first":
            self.current_index = 0
        elif direction == "last":
            self.current_index = len(self.dialogues) - 1

        preview = self._get_current_dialogue_preview()

        # 获取当前对话的翻译状态
        dialogue = self.dialogues[self.current_index]
        translation = dialogue.translated_text if dialogue.is_translated else ""

        return preview, translation

    def translate_current(self, manual_translation: str = "") -> tuple:
        """翻译当前对话"""
        if not self.dialogues:
            return "暂无对话", "暂无对话"

        dialogue = self.dialogues[self.current_index]

        if manual_translation:
            # 手动翻译
            dialogue.translated_text = manual_translation
            dialogue.is_translated = True
        else:
            # AI翻译
            if not self.translator:
                return "❌ 请先配置翻译器", self._get_current_dialogue_preview()

            try:
                translated = self.translator.translate_text(
                    text=dialogue.original_text,
                    character=dialogue.character,
                    context_before=dialogue.context_before,
                    context_after=dialogue.context_after,
                    character_dict=self.char_dict.get_dict()
                )
                dialogue.translated_text = translated
                dialogue.is_translated = True
            except Exception as e:
                return f"❌ 翻译失败: {str(e)}", self._get_current_dialogue_preview()

        preview = self._get_current_dialogue_preview()
        return f"✅ 翻译完成", preview

    def translate_all(self, progress=gr.Progress()) -> str:
        """批量翻译所有对话"""
        if not self.translator:
            return "❌ 请先配置翻译器"

        if not self.dialogues:
            return "暂无对话"

        # 准备翻译数据
        texts_to_translate = []
        for i, dialogue in enumerate(self.dialogues):
            if not dialogue.is_translated:
                texts_to_translate.append({
                    'index': i,
                    'original_text': dialogue.original_text,
                    'character': dialogue.character,
                    'context_before': dialogue.context_before,
                    'context_after': dialogue.context_after
                })

        if not texts_to_translate:
            return "✅ 所有对话已翻译"

        # 批量翻译
        def update_progress(current, total):
            progress(current / total, desc=f"翻译进度: {current}/{total}")

        results = self.translator.translate_batch(
            texts_to_translate,
            character_dict=self.char_dict.get_dict(),
            progress_callback=update_progress
        )

        # 更新对话
        translated_count = 0
        for result in results:
            if result['is_translated']:
                idx = result['index']
                self.dialogues[idx].translated_text = result['translated_text']
                self.dialogues[idx].is_translated = True
                translated_count += 1

        return f"✅ 批量翻译完成！成功: {translated_count}/{len(texts_to_translate)}"

    def update_character_dict(self, dict_text: str) -> str:
        """更新人名词典"""
        try:
            # 解析词典文本
            new_dict = {}
            for line in dict_text.strip().split('\n'):
                if '→' in line:
                    parts = line.split('→')
                    en_name = parts[0].strip()
                    cn_name = parts[1].strip()
                    if en_name and cn_name:
                        new_dict[en_name] = cn_name

            self.char_dict.dictionary = new_dict
            self.char_dict.save()

            return f"✅ 词典已更新，共 {len(new_dict)} 条记录"

        except Exception as e:
            return f"❌ 更新失败: {str(e)}"

    def export_translation(self) -> str:
        """导出翻译结果"""
        if not self.dialogues:
            return "暂无翻译内容"

        # 生成翻译报告
        report = "# Ren'Py翻译报告\n\n"
        report += f"游戏目录: {self.current_game_dir}\n"
        report += f"总对话数: {len(self.dialogues)}\n"

        translated_count = sum(1 for d in self.dialogues if d.is_translated)
        report += f"已翻译数: {translated_count}\n"
        report += f"翻译进度: {translated_count/len(self.dialogues)*100:.1f}%\n\n"

        report += "## 翻译详情\n\n"

        for dialogue in self.dialogues:
            if dialogue.is_translated:
                report += f"### {dialogue.file_path}:{dialogue.line_number}\n"
                report += f"- 角色: {dialogue.character or '旁白'}\n"
                report += f"- 原文: {dialogue.original_text}\n"
                report += f"- 译文: {dialogue.translated_text}\n\n"

        # 保存报告
        report_path = os.path.join(self.current_game_dir, "translation_report.md")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)

        return f"✅ 翻译报告已导出: {report_path}"

    def merge_translation(self) -> str:
        """合并翻译到游戏文件"""
        if not self.dialogues:
            return "暂无翻译内容"

        if not self.current_game_dir:
            return "❌ 未加载游戏目录"

        # 按文件分组
        file_translations = {}
        for dialogue in self.dialogues:
            if dialogue.is_translated:
                if dialogue.file_path not in file_translations:
                    file_translations[dialogue.file_path] = []
                file_translations[dialogue.file_path].append(dialogue)

        merged_count = 0

        for file_path, dialogues in file_translations.items():
            try:
                # 读取原文件
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                # 替换翻译
                for dialogue in dialogues:
                    line_idx = dialogue.line_number - 1
                    if line_idx < len(lines):
                        original_line = lines[line_idx]

                        # 构建新行
                        if dialogue.character:
                            # 角色对话
                            new_line = f'{dialogue.character} "{dialogue.translated_text}"\n'
                        else:
                            # 旁白
                            new_line = f'"{dialogue.translated_text}"\n'

                        lines[line_idx] = new_line
                        merged_count += 1

                # 保存修改后的文件
                backup_path = file_path + '.backup'
                if not os.path.exists(backup_path):
                    # 备份原文件
                    with open(file_path, 'r', encoding='utf-8') as f:
                        backup_content = f.read()
                    with open(backup_path, 'w', encoding='utf-8') as f:
                        f.write(backup_content)

                # 写入翻译后的文件
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)

            except Exception as e:
                return f"❌ 合并失败 {file_path}: {str(e)}"

        return f"✅ 合并完成！已修改 {merged_count} 行对话，原文件已备份为 .backup"


def create_ui():
    """创建Gradio界面"""
    app = RenpyTranslatorApp()

    with gr.Blocks(title="Ren'Py游戏翻译工具") as demo:
        gr.Markdown("# 🎮 Ren'Py游戏翻译工具")
        gr.Markdown("支持AI翻译和手动翻译，让游戏汉化更简单")

        with gr.Tabs():
            # 第一个标签页：翻译配置
            with gr.Tab("⚙️ 翻译配置"):
                gr.Markdown("### AI翻译接口配置")

                with gr.Row():
                    with gr.Column(scale=2):
                        api_base = gr.Textbox(
                            label="API地址",
                            value="https://api.openai.com/v1",
                            placeholder="https://api.openai.com/v1"
                        )
                        api_key = gr.Textbox(
                            label="API Key",
                            type="password",
                            placeholder="sk-..."
                        )
                        model = gr.Textbox(
                            label="模型名称",
                            value="gpt-3.5-turbo",
                            placeholder="gpt-3.5-turbo, claude-3, etc."
                        )

                    with gr.Column(scale=1):
                        temperature = gr.Slider(
                            label="Temperature",
                            minimum=0,
                            maximum=2,
                            value=0.3,
                            step=0.1
                        )
                        max_tokens = gr.Slider(
                            label="最大Token数",
                            minimum=100,
                            maximum=4000,
                            value=1000,
                            step=100
                        )
                        context_lines = gr.Slider(
                            label="上下文行数",
                            minimum=0,
                            maximum=10,
                            value=3,
                            step=1
                        )

                config_btn = gr.Button("💾 保存配置", variant="primary")
                config_status = gr.Textbox(label="配置状态", interactive=False)

                config_btn.click(
                    fn=app.setup_translator,
                    inputs=[api_base, api_key, model, temperature,
                           max_tokens, context_lines],
                    outputs=[config_status]
                )

            # 第二个标签页：游戏加载
            with gr.Tab("📂 加载游戏"):
                gr.Markdown("### 选择游戏目录")

                with gr.Row():
                    game_dir = gr.Textbox(
                        label="游戏目录路径",
                        placeholder="F:\\Games\\MyGame"
                    )
                    include_ui = gr.Checkbox(
                        label="包含界面文字",
                        value=False
                    )

                load_btn = gr.Button("📂 加载游戏", variant="primary")

                with gr.Row():
                    with gr.Column():
                        game_stats = gr.Textbox(
                            label="加载状态",
                            interactive=False,
                            lines=8
                        )
                    with gr.Column():
                        char_list = gr.Textbox(
                            label="角色列表",
                            interactive=False,
                            lines=8
                        )

                dialogue_preview = gr.Textbox(
                    label="对话预览",
                    interactive=False,
                    lines=10
                )

                load_btn.click(
                    fn=app.load_game,
                    inputs=[game_dir, include_ui],
                    outputs=[game_stats, char_list, dialogue_preview]
                )

            # 第三个标签页：翻译工作台
            with gr.Tab("🔤 翻译工作台"):
                gr.Markdown("### 逐句翻译")

                with gr.Row():
                    with gr.Column(scale=2):
                        current_preview = gr.Textbox(
                            label="当前对话",
                            interactive=False,
                            lines=12
                        )

                        with gr.Row():
                            first_btn = gr.Button("⏮️ 第一句")
                            prev_btn = gr.Button("⬅️ 上一句")
                            next_btn = gr.Button("➡️ 下一句")
                            last_btn = gr.Button("⏭️ 最后")

                    with gr.Column(scale=1):
                        translation_input = gr.Textbox(
                            label="翻译输入（留空则AI翻译）",
                            lines=4,
                            placeholder="输入手动翻译，或留空让AI翻译"
                        )

                        with gr.Row():
                            translate_btn = gr.Button(
                                "🤖 AI翻译",
                                variant="primary"
                            )
                            manual_btn = gr.Button(
                                "✏️ 手动翻译",
                                variant="secondary"
                            )

                        translate_status = gr.Textbox(
                            label="翻译状态",
                            interactive=False
                        )

                # 导航按钮事件
                first_btn.click(
                    fn=lambda: app.navigate_dialogue("first"),
                    outputs=[current_preview, translation_input]
                )
                prev_btn.click(
                    fn=lambda: app.navigate_dialogue("prev"),
                    outputs=[current_preview, translation_input]
                )
                next_btn.click(
                    fn=lambda: app.navigate_dialogue("next"),
                    outputs=[current_preview, translation_input]
                )
                last_btn.click(
                    fn=lambda: app.navigate_dialogue("last"),
                    outputs=[current_preview, translation_input]
                )

                # 翻译按钮事件
                translate_btn.click(
                    fn=lambda: app.translate_current(""),
                    outputs=[translate_status, current_preview]
                )
                manual_btn.click(
                    fn=lambda t: app.translate_current(t),
                    inputs=[translation_input],
                    outputs=[translate_status, current_preview]
                )

            # 第四个标签页：批量翻译
            with gr.Tab("⚡ 批量翻译"):
                gr.Markdown("### 批量翻译所有对话")

                with gr.Row():
                    batch_translate_btn = gr.Button(
                        "🚀 开始批量翻译",
                        variant="primary",
                        size="lg"
                    )

                batch_progress = gr.Textbox(
                    label="翻译进度",
                    interactive=False,
                    lines=5
                )

                batch_translate_btn.click(
                    fn=app.translate_all,
                    outputs=[batch_progress]
                )

            # 第五个标签页：人名词典
            with gr.Tab("👤 人名词典"):
                gr.Markdown("### 管理角色翻译词典")

                dict_editor = gr.Textbox(
                    label="人名词典（每行格式：英文名 → 中文名）",
                    lines=15,
                    placeholder="Eileen → 艾琳\nLucy → 露西"
                )

                with gr.Row():
                    load_dict_btn = gr.Button("📂 加载词典")
                    save_dict_btn = gr.Button("💾 保存词典", variant="primary")

                dict_status = gr.Textbox(
                    label="词典状态",
                    interactive=False
                )

                def load_dict():
                    return app.char_dict.get_formatted()

                def save_dict(text):
                    return app.update_character_dict(text)

                load_dict_btn.click(
                    fn=load_dict,
                    outputs=[dict_editor]
                )
                save_dict_btn.click(
                    fn=save_dict,
                    inputs=[dict_editor],
                    outputs=[dict_status]
                )

            # 第六个标签页：导出合并
            with gr.Tab("📦 导出合并"):
                gr.Markdown("### 导出翻译结果")

                with gr.Row():
                    export_btn = gr.Button(
                        "📄 导出翻译报告",
                        variant="secondary"
                    )
                    merge_btn = gr.Button(
                        "🔄 合并到游戏",
                        variant="primary"
                    )

                export_status = gr.Textbox(
                    label="操作状态",
                    interactive=False,
                    lines=5
                )

                export_btn.click(
                    fn=app.export_translation,
                    outputs=[export_status]
                )
                merge_btn.click(
                    fn=app.merge_translation,
                    outputs=[export_status]
                )

        # 使用说明
        with gr.Accordion("📖 使用说明", open=False):
            gr.Markdown("""
            ## 使用步骤

            1. **配置翻译器**
               - 填写OpenAI兼容的API地址和Key
               - 选择模型（如gpt-4、claude-3等）
               - 调整翻译参数

            2. **加载游戏**
               - 输入Ren'Py游戏目录路径
               - 点击加载，查看角色和对话

            3. **翻译对话**
               - 使用翻译工作台逐句翻译
               - 可选择AI翻译或手动翻译
               - 或使用批量翻译功能

            4. **管理人名词典**
               - 编辑人名词典确保翻译一致
               - 格式：英文名 → 中文名

            5. **导出合并**
               - 导出翻译报告
               - 合并翻译到游戏文件（会自动备份）

            ## 注意事项
            - 请确保有游戏汉化授权
            - 翻译前建议备份游戏文件
            - 人名词典有助于保持翻译一致性
            """)

    return demo


if __name__ == "__main__":
    demo = create_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_api=False
    )
