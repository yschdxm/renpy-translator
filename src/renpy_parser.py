"""Ren'Py脚本解析器 - 负责解析和提取游戏文本"""

import re
import os
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class DialogueLine:
    """对话行数据结构"""
    file_path: str
    line_number: int
    character: str  # 角色名（空字符串表示旁白）
    original_text: str
    translated_text: str = ""
    is_translated: bool = False
    context_before: List[str] = None  # 前文上下文
    context_after: List[str] = None   # 后文上下文

    def __post_init__(self):
        if self.context_before is None:
            self.context_before = []
        if self.context_after is None:
            self.context_after = []


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
                    char_info = CharacterInfo(
                        variable=var_name,
                        name=char_name,
                        chinese_name=""
                    )
                    characters.append(char_info)
                    self.characters[var_name] = char_info
        return characters

    def extract_dialogue(self, content: str, file_path: str,
                        context_lines: int = 3) -> List[DialogueLine]:
        """从脚本中提取对话文本"""
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
                        char_name = self.characters.get(char_var, CharacterInfo(char_var, char_var)).name
                    else:
                        # 旁白
                        char_var = ""
                        text = match.group(1)
                        char_name = ""

                    # 获取上下文
                    start_ctx = max(0, line_num - context_lines - 1)
                    end_ctx = min(len(lines), line_num + context_lines)
                    context_before = [l.strip() for l in lines[start_ctx:line_num-1] if l.strip() and not l.strip().startswith('#')]
                    context_after = [l.strip() for l in lines[line_num:end_ctx] if l.strip() and not l.strip().startswith('#')]

                    dialogue = DialogueLine(
                        file_path=file_path,
                        line_number=line_num,
                        character=char_name,
                        original_text=text,
                        context_before=context_before[-context_lines:],
                        context_after=context_after[:context_lines]
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

        # 提取角色
        characters = self.extract_characters(content, file_path)

        # 提取对话
        dialogues = self.extract_dialogue(content, file_path)

        # 提取UI文字（仅对screens.rpy等界面文件）
        ui_texts = []
        if extract_ui or 'screens.rpy' in file_path or 'gui.rpy' in file_path:
            ui_texts = self.extract_ui_text(content, file_path)

        return {
            'file_path': file_path,
            'characters': characters,
            'dialogues': dialogues,
            'ui_texts': ui_texts
        }

    def parse_directory(self, game_dir: str,
                       include_ui: bool = False) -> dict:
        """解析整个游戏目录"""
        game_path = Path(game_dir)
        all_characters = []
        all_dialogues = []
        all_ui_texts = []

        # 查找所有.rpy文件
        rpy_files = list(game_path.rglob('*.rpy'))

        for rpy_file in rpy_files:
            file_str = str(rpy_file)
            # 跳过翻译目录中的文件
            if 'tl' in rpy_file.parts:
                continue

            result = self.parse_file(file_str, extract_ui=include_ui)
            all_characters.extend(result['characters'])
            all_dialogues.extend(result['dialogues'])
            all_ui_texts.extend(result['ui_texts'])

        return {
            'game_dir': game_dir,
            'characters': all_characters,
            'dialogues': all_dialogues,
            'ui_texts': all_ui_texts,
            'total_files': len(rpy_files)
        }

    def decompile_rpyc(self, rpyc_path: str, output_path: Optional[str] = None) -> str:
        """反编译.rpyc文件为.rpy（需要unrpyc）"""
        # 这里需要调用unrpyc工具
        # 实际实现时需要集成unrpyc库
        raise NotImplementedError("需要集成unrpyc库")
