r"""内嵌文本的 strings 表应用路径（apply_path='table'）

把选中的显示类内嵌字符串直接写进 `game/tl/chinese/zz_embedded.rpy` 的
`translate chinese strings:` 块（old "..." / new ""），不改动游戏源码。

为什么表路径是默认（与 _() 包裹的对比）：
- 零源码修改：游戏更新无需重包（rewrap），反编译游戏也不受影响
- 双重用途字符串天然安全：Ren'Py 渲染任何 Text displayable 前都会
  translate_string 查表（substitutions.substitute, translate=True），
  逻辑代码里变量仍持有原文值，== 比较/键查找不受影响；只有显示层
  看到译文。_() 包裹则把逻辑值也译了，比较失效。

文本形态（最易踩的坑，已配闭环测试）：
- 文件形态：old 行里 `\` 写 `\\`、`"` 写 `\"`、真实换行写字面 `\n`
  （escape_tl_old）
- 入库形态（ui_texts.original_text）：`_fill_strings` 与
  `_parse_strings_block` 读 old 行时只反转义 `\"`——所以入库形态 =
  文件形态仅去掉引号转义（to_db_form：`\` 仍双写、换行仍是字面 \n）。
  导出的 translation_dict 以入库形态为 key，两边必须严格一致，
  否则译文永远填不进。
- 重复 old 是 Ren'Py 硬错误（Duplicate translation）：写前必须
  收集其余 tl 文件的 old 集合去重（existing_tl_olds）。
"""
import re
from pathlib import Path

from embedded_strings import resolve_source_root

ZZ_NAME = 'zz_embedded.rpy'

_OLD_LINE_RE = re.compile(r'^\s+old\s+"(.*)"')


def to_db_form(text: str) -> str:
    """候选原文（真实形态）→ 入库形态（`\` 双写、换行字面 \n）"""
    return text.replace('\\', '\\\\').replace('\n', '\\n')


def escape_tl_old(text: str) -> str:
    """候选原文（真实形态）→ tl old 行的文件形态（再补引号转义）"""
    return to_db_form(text).replace('"', '\\"')


def existing_tl_olds(tl_dir: Path) -> set:
    """其余 tl 文件的全部 old 文本（入库形态；排除 ZZ 自身——它是全量重写）"""
    olds = set()
    tl_dir = Path(tl_dir)
    if not tl_dir.is_dir():
        return olds
    for rpy in sorted(tl_dir.rglob('*.rpy')):
        if rpy.name == ZZ_NAME:
            continue
        try:
            lines = rpy.read_text(encoding='utf-8', errors='ignore').split('\n')
        except OSError:
            continue
        for line in lines:
            m = _OLD_LINE_RE.match(line)
            if m:
                olds.add(m.group(1).replace('\\"', '"'))
    return olds


def collect_table_entries(rows: list, source_root: Path,
                          existing_olds: set) -> list:
    """从 table 路径的 marked 行收集可写条目：
    - 源码仍存在（raw 字面量 verbatim 仍在原文件里；消失的不写——
      文本回归后下次 regen 自动恢复）
    - 不在现有 old 集合（重复 old 是 Ren'Py 硬错误）
    - 按入库形态去重（表是全局精确查找，同一文本一条即可）
    """
    entries = []
    seen = set(existing_olds)
    for r in rows:
        path = Path(source_root) / r['rel_file']
        try:
            content = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        if r['raw'] not in content:
            continue
        db_form = to_db_form(r['text'])
        if db_form in seen:
            continue
        seen.add(db_form)
        entries.append({'text': r['text'], 'db_form': db_form,
                        'file': r['rel_file'], 'line': r['line'],
                        'hint': r.get('hint', '')})
    return entries


def write_strings_table(tl_dir: Path, entries: list) -> Path:
    """全量重写 ZZ 文件（格式与 SDK 模板一致，_fill_strings/_parse_strings_block
    已验证可读）；entries 为空时删除文件（无条目不留空壳）。返回文件路径。"""
    tl_dir = Path(tl_dir)
    out = tl_dir / ZZ_NAME
    if not entries:
        out.unlink(missing_ok=True)
        return out
    tl_dir.mkdir(parents=True, exist_ok=True)
    parts = ['translate chinese strings:\n']
    for e in entries:
        parts.append(f'\n    # {e["file"]}:{e["line"]}\n'
                     f'    old "{escape_tl_old(e["text"])}"\n'
                     f'    new ""\n')
    out.write_text('\n'.join(parts), encoding='utf-8')
    return out


def regen_embedded_table(db, game_root: Path, logger=None) -> int:
    """按库里全部 table 路径 marked 行重生成 ZZ 翻译表并入库新条目

    apply_selection 与项目更新共用：全量重写（消失文本自然淘汰，
    行保持 marked 不写文件；文本回归后下次自动恢复）。
    返回新入库的 ui_texts 条数。
    """
    from tl_parser import parse_translation_files
    game_root = Path(game_root)
    source_root = resolve_source_root(game_root)
    tl_dir = game_root / 'game' / 'tl' / 'chinese'

    rows = db.get_table_marked_embedded()
    entries = collect_table_entries(rows, source_root,
                                    existing_tl_olds(tl_dir))
    vanished = len(rows) - len(entries)
    if vanished > 0 and logger:
        logger.warning(
            f'内嵌翻译表: {vanished} 条已标记文本在新源码中未找到'
            '（本次不写入；文本回归后自动恢复）', panel='ui')
    out = write_strings_table(tl_dir, entries)
    if not entries:
        return 0

    # 解析回读（入库形态由解析器决定，与导出填充的 key 形态严格一致），
    # 出处 hint 按入库形态回填 context_hint
    tl_result = parse_translation_files(tl_dir, str(game_root), logger)
    hint_map = {e['db_form']: e['hint'] for e in entries}
    new_items = []
    for it in tl_result.get('ui_texts', []):
        if it['original_text'] in hint_map:
            it['context_hint'] = hint_map[it['original_text']]
            new_items.append(it)
    return db.insert_ui_texts_new_only(new_items)
