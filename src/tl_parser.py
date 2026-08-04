"""SDK 生成的翻译文件（game/tl/chinese/*.rpy）解析

从 project_panel 迁出的共享实现，供项目创建流程与
内嵌文本提取后的合并入库流程共同使用。
"""

import re
from pathlib import Path


def parse_translation_files(tl_dir, game_dir: str, logger=None) -> dict:
    """解析 SDK 生成的翻译文件，返回 {'dialogues': [...], 'ui_texts': [...]}"""
    tl_dir = Path(tl_dir)
    dialogues = []
    ui_texts = []
    game_path = Path(game_dir)

    for tl_file in tl_dir.rglob('*.rpy'):
        try:
            with open(tl_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            dialogue_lines = []
            strings_lines = []
            in_strings = False

            for line in lines:
                if 'translate chinese strings:' in line:
                    in_strings = True
                if in_strings:
                    strings_lines.append(line)
                else:
                    dialogue_lines.append(line)

            if dialogue_lines:
                _parse_dialogue_blocks(dialogue_lines, tl_file, game_path,
                                       dialogues, ui_texts)
            if strings_lines:
                _parse_strings_block(strings_lines, tl_file, game_path, ui_texts)
        except Exception as e:
            if logger:
                logger.error(f"解析翻译文件失败 {tl_file}: {e}")

    return {'dialogues': dialogues, 'ui_texts': ui_texts}


def _parse_dialogue_blocks(lines, tl_file, game_path, dialogues, ui_texts):
    """解析对话格式的翻译块，提取 label 归属"""
    current_file = str(tl_file.relative_to(tl_file.parent.parent.parent))
    if current_file.startswith('tl/chinese/'):
        current_file = current_file[len('tl/chinese/'):]

    current_label = ""
    current_line_no = 0
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if not line:
            continue

        # 提取 label：translate chinese label_hash:
        translate_match = re.match(r'^translate\s+\w+\s+(\w+)\s*:', line)
        if translate_match:
            raw_label = translate_match.group(1)
            # 去掉末尾的 hex hash 段（如 luna_gallery_intro_71d6afad → luna_gallery_intro）
            current_label = re.sub(r'_[0-9a-f]{6,}$', '', raw_label)
            continue

        # 提取行号注释：# game/xxx.rpy:15（在 translate 行之前，作为后续 block 的行号）
        game_line_match = re.match(r'^\s*#\s+game/.+:(\d+)\s*$', line)
        if game_line_match:
            current_line_no = int(game_line_match.group(1))
            continue

        if re.match(r'^\s*#\s+game/', line):
            continue

        comment_match = re.match(r'^\s*#\s*(.*)', line)
        if comment_match:
            comment_text = comment_match.group(1).strip()
            if not comment_text:
                continue
            char_match = re.match(r'^(\w+)\s+"(.*)"', comment_text)
            narration_match = re.match(r'^"(.*)"', comment_text)
            if char_match:
                character = char_match.group(1)
                text = char_match.group(2).replace('\\"', '"')
            elif narration_match:
                character = ''
                text = narration_match.group(1).replace('\\"', '"')
            else:
                continue
            if not text or text == '""':
                if i < len(lines):
                    i += 1
                continue
            if i < len(lines):
                i += 1

            full_path = str(game_path / 'game' / current_file)
            entry = {
                'file_path': full_path, 'line_number': current_line_no,
                'label': current_label,
                'character': character, 'original_text': text,
                'translated_text': '', 'is_translated': False,
            }
            # 只按文件名判断是否为界面文本文件，避免目录名（如 *_screens/）误判
            file_name = current_file.replace('\\', '/').rsplit('/', 1)[-1]
            if any(f in file_name for f in ['screens', 'gui', 'options', 'common']):
                ui_texts.append(entry)
            else:
                dialogues.append(entry)


def _parse_strings_block(lines, tl_file, game_path, ui_texts):
    """解析 strings 格式的翻译块"""
    current_file = None
    current_line = None
    current_old = None

    for line in lines:
        line = line.rstrip()
        file_match = re.match(r'^\s+#\s+(.+):(\d+)', line)
        if file_match:
            current_file = file_match.group(1)
            current_line = int(file_match.group(2))
            continue
        old_match = re.match(r'^\s+old\s+"(.*)"', line)
        if old_match:
            current_old = old_match.group(1).replace('\\"', '"')
            continue
        new_match = re.match(r'^\s+new\s+"(.*)"', line)
        if new_match and current_old is not None:
            full_path = str(game_path / 'game' / current_file) if current_file else ''
            entry = {
                'file_path': full_path, 'line_number': current_line or 0,
                'label': '',
                'character': '', 'original_text': current_old,
                'translated_text': '', 'is_translated': False,
            }
            ui_texts.append(entry)
            current_old = None
