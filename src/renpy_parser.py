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
    label: str = ""  # 所属 label 名


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
        # define e = Character("Eileen") / Character(_("Eileen"))
        r'^define\s+(\w+)\s*=\s*Character\((?:_\()?"([^"]+)"',
        # e = Character("Eileen") / e = Character(_("Eileen"))
        r'^(\w+)\s*=\s*Character\((?:_\()?"([^"]+)"',
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
        """从脚本中提取对话文本，记录每条对话所属的 label"""
        lines = content.split('\n')
        dialogues = []
        current_label = ""

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            # 跳过注释和空行
            if stripped.startswith('#') or not stripped:
                continue

            # 检测 label 定义：label xxx:
            label_match = re.match(r'^label\s+(\w+)\s*:', stripped)
            if label_match:
                current_label = label_match.group(1)
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
                        original_text=text,
                        label=current_label
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

    # ---- UI 字符串出处定位 ----

    # 行类型识别（优先级从上到下）
    _SCOPE_PATTERNS = [
        (re.compile(r'^screen\s+(\w+)'), 'screen'),
        (re.compile(r'^label\s+(\w+)\s*:'), 'label'),
        (re.compile(r'^menu\s*:'), 'menu'),
        (re.compile(r'^menu\s+"((?:[^"\\]|\\.)*?)"\s*:'), 'menu'),
    ]
    _KIND_PATTERNS = [
        (re.compile(r'\btextbutton\s+_\(\s*"((?:[^"\\]|\\.)*?)"\s*\)'), '按钮'),
        (re.compile(r'\btextbutton\s+"((?:[^"\\]|\\.)*?)"'), '按钮'),
        (re.compile(r'\btext\s+_\(\s*"((?:[^"\\]|\\.)*?)"\s*\)'), '界面文本'),
        (re.compile(r'\btooltip\s+"((?:[^"\\]|\\.)*?)"'), '提示'),
        (re.compile(r'\blabel\s+_\(\s*"((?:[^"\\]|\\.)*?)"\s*\)'), '标题'),
        (re.compile(r'^"((?:[^"\\]|\\.)*?)"\s*:'), 'menu选项'),
        (re.compile(r'_\(\s*"((?:[^"\\]|\\.)*?)"\s*\)'), '文本'),
    ]

    @staticmethod
    def _unescape_renpy(s: str) -> str:
        """Ren'Py 字符串反转义（与 SDK old/new 块中的原文对齐）"""
        return (s.replace('\\"', '"').replace("\\'", "'")
                 .replace('\\n', '\n').replace('\\t', '\t')
                 .replace('\\\\', '\\'))

    def locate_ui_string_contexts(self, game_dir: str) -> dict:
        """回扫游戏源码，定位 UI 字符串的出处

        逐行跟踪封闭作用域（screen/label/menu）与行类型（按钮/界面文本/menu选项等），
        提取行内带引号字符串，返回 {反转义后的原文: 出处描述}。
        出处描述格式："screen名·按钮" / "label名·menu选项" / "label名·文本" 等。
        """
        hints = {}
        game_path = Path(game_dir)
        search_roots = [p for p in (game_path / 'game', game_path) if p.exists()]
        exclude_dirs = {'renpy', 'lib', 'saves', 'cache', 'tl', 'audio', 'sound',
                        'images', 'image', 'fonts', 'font', 'video', 'movies'}

        seen_files = set()
        for root in search_roots:
            for rpy_file in root.rglob('*.rpy'):
                if rpy_file in seen_files:
                    continue
                seen_files.add(rpy_file)
                if any(part in exclude_dirs for part in rpy_file.parts):
                    continue
                try:
                    content = rpy_file.read_text(encoding='utf-8', errors='ignore')
                except OSError:
                    continue
                self._scan_file_for_hints(content, hints)
        return hints

    def _scan_file_for_hints(self, content: str, hints: dict):
        """扫描单个文件，把字符串出处写入 hints（已有出处的不覆盖）"""
        scope_stack = []  # [(indent, kind, name)]

        def current_scope():
            return scope_stack[-1] if scope_stack else (0, '', '')

        for raw_line in content.split('\n'):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            indent = len(raw_line) - len(raw_line.lstrip())

            # 弹出缩进不小于当前行的作用域
            while scope_stack and indent <= scope_stack[-1][0]:
                scope_stack.pop()

            # 作用域定义行（screen/label 行本身无可提取字符串，menu "标题" 已在上面记录）
            for pattern, kind in self._SCOPE_PATTERNS:
                m = re.match(pattern, stripped)
                if m:
                    name = m.group(1) if kind != 'menu' or m.lastindex else 'menu'
                    scope_stack.append((indent, kind, name))
                    # menu "标题" 的标题本身也是可翻译字符串
                    if kind == 'menu' and m.lastindex:
                        text = self._unescape_renpy(m.group(1))
                        hints.setdefault(text, f'场景菜单·菜单标题')
                    break

            # 字符串行：识别行类型并提取
            _, scope_kind, scope_name = current_scope()
            for pattern, kind in self._KIND_PATTERNS:
                m = pattern.search(stripped)
                if m:
                    text = self._unescape_renpy(m.group(1))
                    if len(text) > 1 and not text.startswith('$'):
                        if scope_kind == 'screen':
                            hint = f'{scope_name}界面·{kind}'
                        elif scope_kind == 'label':
                            hint = f'{scope_name}场景·{kind}'
                        elif scope_kind == 'menu':
                            hint = f'{scope_name}·{kind}' if kind != 'menu选项' else 'menu选项'
                        else:
                            hint = kind
                        hints.setdefault(text, hint)
                    break
