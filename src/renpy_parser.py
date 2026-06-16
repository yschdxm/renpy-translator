"""Ren'Py脚本解析器 - 负责解析和提取游戏文本"""

import re
import os
import sys
import struct
import subprocess
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
                        char_name = self.characters.get(char_var, CharacterInfo(char_var, char_var)).name
                    else:
                        # 旁白
                        char_var = ""
                        text = match.group(1)
                        char_name = ""

                    # 跳过空文本
                    if not text.strip():
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
                       include_ui: bool = False,
                       extract_rpa: bool = True,
                       decompile_rpyc: bool = True) -> dict:
        """解析整个游戏目录"""
        from rpa_extractor import RPAExtractor

        game_path = Path(game_dir)
        all_characters = []
        all_dialogues = []
        all_ui_texts = []
        extracted_files = 0

        # 需要排除的目录（引擎和库文件）
        exclude_dirs = {'renpy', 'lib', 'saves', 'cache', 'audio', 'images', 'fonts',
                       'video1', 'video2', 'video3', 'video_demo', 'tl'}

        # 自动解包.rpa文件
        if extract_rpa:
            rpa_files = list(game_path.glob('*.rpa')) + list((game_path / 'game').glob('*.rpa'))
            if rpa_files:
                print(f"找到 {len(rpa_files)} 个.rpa文件，正在解包...")
                extractor = RPAExtractor()
                for rpa_file in rpa_files:
                    try:
                        output_dir = game_path / 'game' / rpa_file.stem
                        print(f"解包到: {output_dir}")
                        extracted = extractor.extract_rpa(str(rpa_file), str(output_dir))
                        if extracted:
                            print(f"成功解包 {len(extracted)} 个文件")
                            extracted_files += 1
                        else:
                            print(f"解包失败: 没有文件被提取")
                    except Exception as e:
                        print(f"解包 {rpa_file.name} 失败: {e}")

        # 自动反编译.rpyc文件
        if decompile_rpyc:
            rpyc_files = list((game_path / 'game').rglob('*.rpyc'))
            if rpyc_files:
                print(f"找到 {len(rpyc_files)} 个.rpyc文件，正在反编译...")
                for rpyc_file in rpyc_files:
                    try:
                        self._decompile_rpyc(str(rpyc_file))
                    except Exception as e:
                        print(f"反编译 {rpyc_file.name} 失败: {e}")

        # 查找所有.rpy文件
        game_subdir = game_path / 'game'
        if game_subdir.exists():
            search_path = game_subdir
        else:
            search_path = game_path

        rpy_files = []
        for rpy_file in search_path.rglob('*.rpy'):
            # 检查是否在排除目录中
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

    def _decompile_rpyc(self, rpyc_path: str) -> Optional[str]:
        """反编译.rpyc文件为.rpy"""
        rpyc_path = Path(rpyc_path)
        rpy_path = rpyc_path.with_suffix('.rpy')

        # 如果.rpy文件已存在，跳过
        if rpy_path.exists():
            return str(rpy_path)

        # 尝试使用unrpyc反编译
        try:
            # 方法1: 使用unrpyc命令行工具
            result = subprocess.run(
                ['unrpyc', str(rpyc_path)],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0 and rpy_path.exists():
                return str(rpy_path)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # 方法2: 使用Python直接解析（简化版）
        try:
            with open(rpyc_path, 'rb') as f:
                data = f.read()

            # 检查文件头
            if data[:4] != b'RENP':
                return None

            # 提取Python版本信息
            version = struct.unpack('<I', data[4:8])[0]

            # 简单的文本提取（用于基本对话）
            # 注意：这是一个简化的实现，可能不适用于所有情况
            text_data = data[8:]

            # 尝试提取UTF-8字符串
            strings = []
            current = b''
            for byte in text_data:
                if 32 <= byte <= 126 or byte >= 192:  # 可打印字符或UTF-8开始
                    current += bytes([byte])
                else:
                    if len(current) > 5:  # 只保留较长的字符串
                        try:
                            s = current.decode('utf-8', errors='ignore')
                            if s.strip():
                                strings.append(s)
                        except:
                            pass
                    current = b''

            # 生成.rpy文件
            if strings:
                with open(rpy_path, 'w', encoding='utf-8') as f:
                    f.write("# 从.rpyc文件自动提取\n")
                    f.write("# 注意：这是简化提取，可能不完整\n\n")
                    for s in strings:
                        if any(c in s for c in ['"', "'", '(', ')', '=']):
                            f.write(f"{s}\n")
                return str(rpy_path)

        except Exception as e:
            print(f"反编译失败: {e}")

        return None

    def decompile_rpyc(self, rpyc_path: str, output_path: Optional[str] = None) -> str:
        """反编译.rpyc文件为.rpy（需要unrpyc）"""
        # 这里需要调用unrpyc工具
        # 实际实现时需要集成unrpyc库
        raise NotImplementedError("需要集成unrpyc库")
