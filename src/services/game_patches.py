"""游戏补丁注册表：导出时对导出副本应用的游戏代码修正

源码只读原则的一部分：某些游戏的代码本身与"翻译时显示译文、逻辑
保持英文"不兼容（典型如把显示串同时当返回值比较），需要极小的
代码修正。这类修正不写入工作副本（保持原始），只在导出副本上应用。

补丁结构：文件 + 锚点文本 + transform。锚点精确匹配才替换——
文件缺失或锚点找不到（游戏版本不同）时跳过并记日志，绝不硬改。

新增补丁规则：锚点取目标游戏原始文件中的精确原文行；transform
在英文（未翻译）环境下必须是恒等改写（保持游戏原生行为不变）。
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GamePatch:
    name: str            # 展示名（日志）
    rel_file: str        # 相对游戏脚本根（game/game/）的路径
    anchor: str          # 必须精确存在的原文文本
    replacement: str     # 替换文本（恒等于锚点则为断言性补丁，无意义——总应有修正）
    count: int = 1       # 期望替换次数（≠实际时记日志）


# ---- 补丁列表 ----
PATCHES: list = [
    # Lab Rats 2（Reformulate）：ClimaxController 把显示串当返回值给
    # 调用方与裸英文比较（the_choice == "Cum inside her" 等 92 个调用
    # 点）。改成显示时现译：菜单选项显示译文，返回值保持英文——
    # 显示与逻辑职责分离。英文环境下 _(x) == x，原生行为不变。
    GamePatch(
        name='ClimaxController 显示时现译',
        rel_file='major_game_classes/game_logic/ClimaxController_ren.py',
        anchor='            display_name = climax_option[0]\n',
        replacement=(
            '            display_name = _(climax_option[0])'
            '  # 翻译补丁：显示时现译，返回值保持英文供逻辑比较\n'),
    ),
    # Lab Rats 2：角色创建界面的占位符默认值。原版赋值与清空判断
    # 两侧一个是 _() 一个不是，翻译后比较恒假、占位符不清空。
    # 两侧统一为 _()（英文环境恒等）。
    GamePatch(
        name='角色创建占位符判断统一 _()（名）',
        rel_file='game_screens/character_screens/mc_creation_ui.rpy',
        anchor='        if name == "Input Your First Name":',
        replacement='        if name == _("Input Your First Name"):'),
    GamePatch(
        name='角色创建占位符判断统一 _()（姓）',
        rel_file='game_screens/character_screens/mc_creation_ui.rpy',
        anchor='        if l_name == "Input Your Last Name":',
        replacement='        if l_name == _("Input Your Last Name"):'),
    GamePatch(
        name='角色创建占位符判断统一 _()（公司名）',
        rel_file='game_screens/character_screens/mc_creation_ui.rpy',
        anchor='        if b_name == "Input Your Business Name":',
        replacement='        if b_name == _("Input Your Business Name"):'),
    # Lab Rats 2：IT 项目悬浮提示的哨兵值。同上：判断与赋值统一 _()。
    GamePatch(
        name='IT 项目哨兵值判断统一 _()',
        rel_file='people/Ellie/IT_Project_Screen.rpy',
        anchor='                    if proj_desc != "Unassigned!":',
        replacement='                    if proj_desc != _("Unassigned!"):'),
]


def apply_game_patches(export_dir: Path, log) -> None:
    """对导出副本应用全部游戏补丁（缺失/锚点不匹配跳过+日志）"""
    from embedded_strings import resolve_source_root
    source_root = resolve_source_root(Path(export_dir) / 'game')
    applied = 0
    for patch in PATCHES:
        path = source_root / patch.rel_file
        if not path.exists():
            log(f'游戏补丁跳过（文件不存在）: {patch.name}'
                f'（{patch.rel_file}）')
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except OSError as e:
            log(f'游戏补丁跳过（读取失败）: {patch.name}: {e}')
            continue
        if patch.anchor not in text:
            log(f'游戏补丁跳过（锚点不匹配，游戏版本可能不同）: {patch.name}')
            continue
        path.write_text(text.replace(patch.anchor, patch.replacement,
                                     patch.count), encoding='utf-8')
        applied += 1
        log(f'已应用游戏补丁: {patch.name}')
    if applied:
        log(f'游戏补丁合计 {applied} 处')
