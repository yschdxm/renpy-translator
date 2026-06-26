"""Ren'Py脚本解析器 - 负责解析和提取游戏文本"""

import re
import os
import sys
import struct
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@dataclass
class DialogueLine:
    """对话行数据结构"""
    file_path: str
    line_number: int
    character: str  # 角色名（空字符串表示旁白）
    original_text: str
    translated_text: str = ""
    is_translated: bool = False


@dataclass
class CharacterInfo:
    """角色信息"""
    variable: str  # 变量名，如 "e"
    name: str      # 角色名，如 "Eileen"
    chinese_name: str = ""  # 中文名


class RenpyParser:
    """Ren'Py脚本解析器"""

    # Ren'Py对话模式
    DIALOGUE_PATTERNS = [
        # 角色对话: e "Hello"
        r'^(\w+)\s+"((?:[^"\\]|\\.)*?)"',
        # 旁白对话: "Hello"
        r'^"((?:[^"\\]|\\.)*?)"',
        # nvl模式对话: e "Hello" nvl_narrator
        r'^(\w+)\s+"((?:[^"\\]|\\.)*?)"\s+nvl_narrator',
    ]

    # 代码关键字（这些不是角色名）
    CODE_KEYWORDS = {
        'textbutton', 'text', 'label', 'hbox', 'vbox', 'frame', 'bar', 'button',
        'image', 'show', 'hide', 'scene', 'play', 'stop', 'queue', 'voice',
        'with', 'pause', 'jump', 'call', 'return', 'menu', 'if', 'elif', 'else',
        'while', 'for', 'pass', 'init', 'default', 'define', 'transform',
        'screen', 'style', 'python', 'call', 'jump', 'return', 'menu',
        'nvl', 'nvl_clear', 'nvl_narrator', 'nvl_mode', 'nvl_function',
    }

    # 变量占位符模式
    VARIABLE_PATTERNS = [
        r'^\[.*\]$',  # [variable]
        r'^\{.*\}$',  # {variable}
        r'^\$.*',     # $python_code
    ]

    # 角色定义模式
    CHARACTER_PATTERNS = [
        # define e = Character("Eileen")
        r'^define\s+(\w+)\s*=\s*Character\("([^"]+)"\)',
        # define e = Character("Eileen", color="#c8ffc8")
        r'^define\s+(\w+)\s*=\s*Character\("([^"]+)".*\)',
        # e = Character("Eileen")
        r'^(\w+)\s*=\s*Character\("([^"]+)"\)',
    ]

    # 界面文字模式（screens.rpy中的字符串）
    UI_TEXT_PATTERNS = [
        # text "Start Game"
        r'text\s+"((?:[^"\\]|\\.)*?)"',
        # label "Start Game"
        r'label\s+"((?:[^"\\]|\\.)*?)"',
        # tooltip "Click here"
        r'tooltip\s+"((?:[^"\\]|\\.)*?)"',
        # 其他UI字符串
        r'"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"',
    ]

    def __init__(self):
        self.characters: dict[str, CharacterInfo] = {}
        self.dialogue_lines: List[DialogueLine] = []
        self.ui_texts: List[DialogueLine] = []

    def extract_characters(self, content: str, file_path: str) -> List[CharacterInfo]:
        """从脚本中提取角色定义"""
        characters = []
        for line_num, line in enumerate(content.split('\n'), 1):
            line = line.strip()
            for pattern in self.CHARACTER_PATTERNS:
                match = re.match(pattern, line)
                if match:
                    var_name = match.group(1)
                    char_name = match.group(2)
                    # 检查是否已经存在该角色
                    if var_name not in self.characters:
                        char_info = CharacterInfo(
                            variable=var_name,
                            name=char_name,
                            chinese_name=""
                        )
                        characters.append(char_info)
                        self.characters[var_name] = char_info
        return characters

    def extract_dialogue(self, content: str, file_path: str) -> List[DialogueLine]:
        """从脚本中提取对话文本（上下文将在显示时动态计算）"""
        lines = content.split('\n')
        dialogues = []

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            # 跳过注释和空行
            if stripped.startswith('#') or not stripped:
                continue

            # 检查角色对话模式
            for pattern in self.DIALOGUE_PATTERNS[:2]:  # 前两个模式
                match = re.match(pattern, stripped)
                if match:
                    if len(match.groups()) == 2:
                        # 角色对话
                        char_var = match.group(1)
                        text = match.group(2)

                        # 过滤掉代码关键字
                        if char_var.lower() in self.CODE_KEYWORDS:
                            break

                        char_name = self.characters.get(char_var, CharacterInfo(char_var, char_var)).name
                    else:
                        # 旁白
                        char_var = ""
                        text = match.group(1)
                        char_name = ""

                    # 跳过空文本
                    if not text.strip():
                        break

                    # 过滤掉变量占位符
                    is_variable = False
                    for vp in self.VARIABLE_PATTERNS:
                        if re.match(vp, text.strip()):
                            is_variable = True
                            break
                    if is_variable:
                        break

                    # 过滤掉包含代码的文本
                    if any(code in text for code in ['config.', 'gui.', 'style_', 'action ', 'Function(', 'Preference(']):
                        break

                    dialogue = DialogueLine(
                        file_path=file_path,
                        line_number=line_num,
                        character=char_name,
                        original_text=text
                    )
                    dialogues.append(dialogue)
                    break

        return dialogues

    def extract_ui_text(self, content: str, file_path: str) -> List[DialogueLine]:
        """从界面文件中提取UI文字"""
        lines = content.split('\n')
        ui_texts = []

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            # 跳过注释
            if stripped.startswith('#'):
                continue

            # 提取UI字符串
            for pattern in self.UI_TEXT_PATTERNS:
                matches = re.finditer(pattern, stripped)
                for match in matches:
                    text = match.group(1)
                    # 过滤掉太短的或纯代码的字符串
                    if len(text) > 1 and not text.startswith('$'):
                        ui_text = DialogueLine(
                            file_path=file_path,
                            line_number=line_num,
                            character="[UI]",
                            original_text=text
                        )
                        ui_texts.append(ui_text)

        return ui_texts

    def parse_file(self, file_path: str, extract_ui: bool = False) -> dict:
        """解析单个.rpy文件"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            # 尝试其他编码
            with open(file_path, 'r', encoding='gbk', errors='ignore') as f:
                content = f.read()

        # 判断是否是配置文件（screens.rpy, gui.rpy, options.rpy等）
        file_name = Path(file_path).name.lower()
        is_config_file = file_name in ['screens.rpy', 'gui.rpy', 'options.rpy', 'common.rpy']

        # 提取角色（所有文件都需要）
        characters = self.extract_characters(content, file_path)

        # 提取对话（配置文件不提取对话）
        dialogues = []
        if not is_config_file:
            dialogues = self.extract_dialogue(content, file_path)

        # 提取UI文字（配置文件或用户要求时提取）
        ui_texts = []
        if extract_ui or is_config_file:
            ui_texts = self.extract_ui_text(content, file_path)

        return {
            'file_path': file_path,
            'characters': characters,
            'dialogues': dialogues,
            'ui_texts': ui_texts
        }

    def parse_directory(self, game_dir: str,
                       include_ui: bool = False,
                       extract_rpa: bool = True,
                       work_dir: str = None) -> dict:
        """解析整个游戏目录

        Args:
            game_dir: 游戏目录路径
            include_ui: 是否包含UI文字
            extract_rpa: 是否解包rpa文件
            work_dir: 工作目录（用于存放临时文件，不修改原游戏）
        """
        from rpa_extractor import RPAExtractor

        game_path = Path(game_dir)

        # 如果指定了工作目录，使用工作目录；否则使用游戏目录
        if work_dir:
            work_path = Path(work_dir)
            work_path.mkdir(parents=True, exist_ok=True)
        else:
            work_path = game_path

        all_characters = []
        all_dialogues = []
        all_ui_texts = []
        extracted_files = 0

        # 需要排除的目录（通用规则）
        # 1. Ren'Py 引擎目录
        # 2. 资源目录（音频、图片、视频、字体）
        # 3. 缓存和临时目录
        # 4. 翻译目录
        exclude_dirs = {
            'renpy',           # Ren'Py 引擎
            'lib',             # 库文件
            'saves',           # 存档
            'cache',           # 缓存
            'tl',              # 翻译目录
            'audio', 'sound',  # 音频
            'images', 'image', # 图片
            'fonts', 'font',   # 字体
            'video', 'movies', # 视频
        }

        # 自动解包.rpa文件（解包到 game/ 目录）
        if extract_rpa:
            rpa_files = list(game_path.glob('*.rpa')) + list((game_path / 'game').glob('*.rpa'))
            if rpa_files:
                print(f"找到 {len(rpa_files)} 个.rpa文件，正在解包...")
                extractor = RPAExtractor()
                for rpa_file in rpa_files:
                    try:
                        # 解包到 game/ 目录（rpa 内部已有目录结构）
                        output_dir = game_path / 'game'
                        print(f"解包 {rpa_file.name} 到: {output_dir}")
                        extracted = extractor.extract_rpa(str(rpa_file), str(output_dir))
                        if extracted:
                            print(f"成功解包 {len(extracted)} 个文件")
                            extracted_files += 1
                        else:
                            print(f"解包失败: 没有文件被提取")
                    except Exception as e:
                        print(f"解包 {rpa_file.name} 失败: {e}")

        # 查找所有.rpy文件（优先使用工作目录中的文件）
        rpy_files = []

        # 先搜索工作目录
        work_game_subdir = work_path / 'game'
        if work_game_subdir.exists():
            for rpy_file in work_game_subdir.rglob('*.rpy'):
                parts = rpy_file.relative_to(work_path).parts
                if any(part in exclude_dirs for part in parts):
                    continue
                rpy_files.append(rpy_file)

        # 再搜索原游戏目录（排除已在工作目录中找到的文件）
        game_subdir = game_path / 'game'
        if game_subdir.exists():
            for rpy_file in game_subdir.rglob('*.rpy'):
                # 检查是否已存在于工作目录
                if work_path != game_path:
                    try:
                        relative = rpy_file.relative_to(game_path)
                        if (work_path / relative).exists():
                            continue
                    except:
                        pass

                parts = rpy_file.relative_to(game_path).parts
                if any(part in exclude_dirs for part in parts):
                    continue
                rpy_files.append(rpy_file)

        print(f"找到 {len(rpy_files)} 个.rpy文件")

        for rpy_file in rpy_files:
            file_str = str(rpy_file)
            result = self.parse_file(file_str, extract_ui=include_ui)
            all_characters.extend(result['characters'])
            all_dialogues.extend(result['dialogues'])
            all_ui_texts.extend(result['ui_texts'])

        return {
            'game_dir': game_dir,
            'characters': all_characters,
            'dialogues': all_dialogues,
            'ui_texts': all_ui_texts,
            'total_files': len(rpy_files),
            'extracted_rpa': extracted_files
        }
