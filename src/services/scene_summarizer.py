"""AI 场景语义：为聚合后的场景批量生成中文标题与一句话摘要

每批 25 个场景一次调用，3 线程并发（复用 ai_screener 的模式）。
输入线索：label 链、首条台词原文、出场人物显示名、台词数、文件名。
容错（与翻译批一致）：整批解析失败重试 2 次 → 仍失败拆半递归 →
单个场景仍失败则跳过（留空标题，前端回退 scene_id）——模型风控
拒答只丢极少数场景，不再整图构建中断。
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

BATCH = 25
CONCURRENCY = 3
MAX_RETRIES = 2

_PROMPT = """你是视觉小说剧情分析专家。以下是从 Ren'Py 游戏静态解析出的剧情场景（JSON 数组），每项含：
- id: 场景标识（label 名）
- labels: 场景包含的连续 label 链
- first: 场景首条台词原文
- speakers: 出场角色
- lines: 台词数
- file: 所在脚本文件

请为每个场景生成：
- title: 中文标题（≤10 字，概括这段剧情，如"入学初遇""深夜密谈"；不要直译 label 名）
- summary: 中文剧情梗概（50-100 字），覆盖：谁出场、在哪里/什么情境、
  发生了什么、这段剧情对主线的意义；基于台词与上下文合理归纳，
  信息不足时可推测但不要编造具体人名

只输出 JSON 数组，不要输出任何其他文字：[{{"id": "...", "title": "...", "summary": "..."}}, ...]

场景清单：
{scenes_json}"""


def summarize_scenes(scenes: list, translator,
                     char_name: Callable[[str], str],
                     progress: Callable[[float, str], None] = None,
                     cancel_event=None) -> dict:
    """批量生成场景标题/摘要，返回 {scene_id: {'title','summary'}}

    scenes: scene_builder.Scene 列表（只用 scene_id/labels/first_text/
    speakers/dialogue_count/file_path 字段）。
    """
    if translator is None:
        return {}

    items = []
    for s in scenes:
        items.append({
            'id': s.scene_id,
            'labels': s.labels[:4],
            'first': s.first_text[:100],
            'speakers': [char_name(v) for v in s.speakers[:6]],
            'lines': s.dialogue_count,
            'file': s.file_path,
        })

    def _call(batch):
        prompt = _PROMPT.format(
            scenes_json=json.dumps(batch, ensure_ascii=False))
        result = translator.analyze_text(
            prompt, max_tokens=max(translator.config.max_tokens, 8000))
        m = re.search(r'\[.*\]', result, re.S)
        if not m:
            raise ValueError(f'场景摘要返回无法解析: {result[:100]}')
        out = {}
        for entry in json.loads(m.group(0)):
            if isinstance(entry, dict) and entry.get('id'):
                out[str(entry['id'])] = {
                    'title': str(entry.get('title', ''))[:30],
                    'summary': str(entry.get('summary', ''))[:300],
                }
        if not out:
            raise ValueError('场景摘要返回为空')
        return out

    def _run_batch(batch) -> dict:
        """重试 → 拆半递归 → 单场景仍失败跳过"""
        last_err = None
        for _ in range(MAX_RETRIES + 1):
            try:
                return _call(batch)
            except Exception as e:  # 网络/解析/风控统一按可重试处理
                last_err = e
        if len(batch) > 1:
            mid = len(batch) // 2
            out = _run_batch(batch[:mid])
            out.update(_run_batch(batch[mid:]))
            return out
        # 单场景彻底失败：跳过（标题留空，前端回退 scene_id）
        print(f'[场景摘要] 跳过场景 {batch[0]["id"]}: {last_err}')
        return {}

    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]
    results = {}
    cache_before = (translator.cache_hit_total,
                    translator.cache_prompt_total)
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(_run_batch, b): b for b in batches}
        done = 0
        for fut in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('剧情图构建已取消')
            results.update(fut.result())
            done += 1
            if progress:
                progress(done / len(batches),
                         f'AI 场景标题 {done}/{len(batches)} 批')
    hit, total = translator.cache_stats_delta(cache_before)
    if total:
        print(f'[场景摘要] prompt 缓存命中 {hit}/{total} tokens'
              f'（{100 * hit / total:.0f}%）')
    return results
