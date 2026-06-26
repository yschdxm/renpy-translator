"""统一翻译调度服务

单句翻译、批量翻译、全部翻译共用同一套逻辑。
翻译结果立即写入 SQLite（毫秒级），不再依赖整体保存。
"""

import asyncio
from typing import Optional, Callable, Any
from concurrent.futures import ThreadPoolExecutor

from translator import AITranslator
from database import ProjectDatabase
from logger import TranslationLogger


class TranslationService:
    """统一翻译调度服务

    - 单条翻译：翻译完成后立即 UPDATE SQLite（毫秒级）
    - 批量翻译：使用事务批量提交，支持进度回调和取消
    - 并发控制：通过 asyncio.Semaphore 限制并发数
    """

    def __init__(self, translator: AITranslator, db: ProjectDatabase,
                 logger: TranslationLogger, max_concurrent: int = 5):
        self.translator = translator
        self.db = db
        self.logger = logger
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cancel_event = asyncio.Event()
        self._running = False
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)

    @property
    def is_running(self) -> bool:
        return self._running

    async def translate_single(self, item_id: int, content_type: str,
                                original_text: str, character: str = '',
                                context_before: list = None,
                                context_after: list = None) -> bool:
        """翻译单条内容 -> 立即写入 SQLite

        Args:
            item_id: 数据库 ID
            content_type: 'name' / 'ui' / 'dialogue'
            original_text: 原文
            character: 角色名（对话翻译时使用）
            context_before: 前文上下文
            context_after: 后文上下文

        Returns:
            是否翻译成功
        """
        self.logger.info(f"开始翻译: {original_text[:30]}...", panel=content_type)

        async with self._semaphore:
            loop = asyncio.get_event_loop()
            try:
                # 获取人名词典（在线程池中）
                char_dict = await loop.run_in_executor(None, self.db.get_char_dict)

                # 翻译（在线程池中）
                translated = await loop.run_in_executor(
                    self._executor,
                    lambda: self._translate_one_sync(
                        text=original_text,
                        content_type=content_type,
                        character=character,
                        context_before=context_before,
                        context_after=context_after,
                        char_dict=char_dict,
                    )
                )

                if translated:
                    # 立即写入 SQLite（在线程池中）
                    def _save():
                        if content_type == 'name':
                            self.db.update_char_name(original_text, translated)
                        elif content_type == 'ui':
                            self.db.update_ui_text(item_id, translated)
                        elif content_type == 'dialogue':
                            self.db.update_dialogue(item_id, translated)

                    await loop.run_in_executor(None, _save)

                    self.logger.info(
                        f"翻译完成: {original_text[:20]} -> {translated[:20]}",
                        panel=content_type
                    )
                    return True
                else:
                    self.logger.warning(f"翻译返回空结果: {original_text[:30]}", panel=content_type)
                    return False

            except Exception as e:
                self.logger.error(f"翻译失败: {original_text[:30]} - {e}", panel=content_type)
                return False

    async def translate_batch(self, content_type: str,
                              items: list[dict] = None,
                              progress_callback: Callable[[int, int, str], None] = None,
                              concurrency: int = 5) -> dict:
        """批量翻译（并发处理）

        Args:
            content_type: 'name' / 'ui' / 'dialogue'
            items: 要翻译的条目列表（None 表示翻译所有未翻译的）
            progress_callback: 进度回调 (current, total, status_text)
            concurrency: 并发数，默认5

        Returns:
            {'success': int, 'failed': int, 'stopped': bool}
        """
        self._cancel_event.clear()
        self._running = True

        try:
            # 获取待翻译列表（DB 查询在线程池中）
            if items is None:
                loop = asyncio.get_event_loop()
                items = await loop.run_in_executor(
                    None, self._get_all_untranslated, content_type
                )

            total = len(items)
            if total == 0:
                return {'success': 0, 'failed': 0, 'stopped': False}

            self.logger.info(
                f"批量翻译开始: {total} 条, 并发{concurrency} ({content_type})",
                panel=content_type
            )

            success_count = 0
            failed_count = 0
            completed_count = 0
            lock = asyncio.Lock()

            async def _translate_one(item: dict) -> bool:
                """翻译单条（带取消检查）"""
                if self._cancel_event.is_set():
                    return False
                ok = await self.translate_single(
                    item_id=item['id'],
                    content_type=content_type,
                    original_text=item.get('original_text', item.get('en_name', '')),
                    character=item.get('character', ''),
                    context_before=item.get('context_before'),
                    context_after=item.get('context_after'),
                )
                return ok

            # 按并发数分批处理
            for batch_start in range(0, total, concurrency):
                if self._cancel_event.is_set():
                    break

                batch = items[batch_start:batch_start + concurrency]

                # 并发执行当前批次
                tasks = [_translate_one(item) for item in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # 统计结果
                async with lock:
                    for result in results:
                        completed_count += 1
                        if isinstance(result, Exception):
                            failed_count += 1
                        elif result:
                            success_count += 1
                        else:
                            failed_count += 1

                # 更新进度
                if progress_callback:
                    progress_callback(
                        completed_count, total,
                        f"翻译中: {completed_count}/{total}"
                    )

            stopped = self._cancel_event.is_set()
            if stopped:
                self.logger.warning(
                    f"翻译已停止: 成功 {success_count}/{total}", panel=content_type
                )
            else:
                self.logger.info(
                    f"批量翻译完成: 成功 {success_count}, 失败 {failed_count}",
                    panel=content_type
                )

            return {
                'success': success_count,
                'failed': failed_count,
                'stopped': stopped
            }

        finally:
            self._running = False

    async def stop(self):
        """停止翻译"""
        self._cancel_event.set()
        self.logger.info("正在停止翻译...", panel="")

    def _get_all_untranslated(self, content_type: str) -> list[dict]:
        """获取所有未翻译的条目"""
        if content_type == 'name':
            names = self.db.get_untranslated_names()
            return [{'id': 0, 'en_name': en, 'original_text': en} for en, _ in names]
        elif content_type == 'ui':
            return self.db.get_untranslated_ui_texts()
        elif content_type == 'dialogue':
            return self.db.get_untranslated_dialogues()
        return []

    def _translate_one_sync(self, text: str, content_type: str,
                            character: str = '',
                            context_before: list = None,
                            context_after: list = None,
                            char_dict: dict = None) -> str:
        """在线程池中执行的单条翻译（同步方法）"""
        if content_type == 'name':
            return self.translator.translate_name(text, debug=False)
        elif content_type == 'ui':
            return self.translator.translate_ui(text, character_dict=char_dict, debug=False)
        elif content_type == 'dialogue':
            return self.translator.translate_text(
                text=text,
                character=character,
                context_before=context_before,
                context_after=context_after,
                character_dict=char_dict,
                debug=False
            )
        return ""
