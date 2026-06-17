"""Ren'Py SDK 管理器 - 负责调用 Ren'Py SDK 功能"""

import os
import sys
import subprocess
from pathlib import Path
from typing import Optional


class SDKManager:
    """Ren'Py SDK 管理器"""

    def __init__(self, sdk_path: str = ""):
        self.sdk_path = Path(sdk_path) if sdk_path else None

    def find_sdk(self, search_dir: str = None) -> Optional[Path]:
        """自动查找 Ren'Py SDK

        Args:
            search_dir: 搜索目录，默认为项目目录

        Returns:
            SDK 路径或 None
        """
        # 1. 检查配置的路径
        if self.sdk_path and self.sdk_path.exists():
            if self._is_valid_sdk(self.sdk_path):
                return self.sdk_path

        # 2. 在指定目录或项目目录中查找
        search_paths = []
        if search_dir:
            search_paths.append(Path(search_dir))

        # 添加常见位置
        search_paths.extend([
            Path.cwd(),
            Path(__file__).parent.parent,  # 项目根目录
            Path.home() / "Downloads",
        ])

        for search_path in search_paths:
            if not search_path.exists():
                continue

            # 查找 renpy-*-sdk 目录
            for item in search_path.iterdir():
                if item.is_dir() and 'renpy' in item.name.lower() and 'sdk' in item.name.lower():
                    if self._is_valid_sdk(item):
                        return item

        return None

    def _is_valid_sdk(self, path: Path) -> bool:
        """检查是否是有效的 Ren'Py SDK"""
        # 检查必要的文件是否存在
        renpy_exe = self.get_renpy_exe(path)
        return renpy_exe.exists()

    def get_renpy_exe(self, sdk_path: Path = None) -> Path:
        """获取 renpy 可执行文件路径"""
        if sdk_path is None:
            sdk_path = self.sdk_path

        if sys.platform == 'win32':
            return sdk_path / 'renpy.exe'
        else:
            return sdk_path / 'renpy.sh'

    def generate_translations(self, game_dir: str, language: str = "chinese") -> dict:
        """调用 Ren'Py 生成翻译文件

        Args:
            game_dir: 游戏目录路径
            language: 目标语言

        Returns:
            {'success': bool, 'message': str, 'output': str}
        """
        if not self.sdk_path:
            return {'success': False, 'message': '未配置 Ren\'Py SDK 路径', 'output': ''}

        renpy_exe = self.get_renpy_exe()
        if not renpy_exe.exists():
            return {'success': False, 'message': f'找不到 {renpy_exe}', 'output': ''}

        try:
            # 构建命令 - 使用正确的 translate 命令
            cmd = [str(renpy_exe), str(game_dir), "translate", language]

            print(f'[SDK] 执行命令: {" ".join(cmd)}')

            # 执行命令
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,  # 增加超时时间
                cwd=str(game_dir)  # 在游戏目录中运行
            )

            output = result.stdout + result.stderr
            print(f'[SDK] 输出:\n{output}')

            if result.returncode == 0:
                return {
                    'success': True,
                    'message': f'成功生成 {language} 翻译文件',
                    'output': output
                }
            else:
                return {
                    'success': False,
                    'message': f'生成失败 (返回码: {result.returncode})',
                    'output': output
                }

        except subprocess.TimeoutExpired:
            return {'success': False, 'message': '执行超时', 'output': ''}
        except Exception as e:
            return {'success': False, 'message': str(e), 'output': ''}

    def list_languages(self, game_dir: str) -> list:
        """列出已有的翻译语言"""
        tl_dir = Path(game_dir) / 'game' / 'tl'
        if not tl_dir.exists():
            return []

        languages = []
        for item in tl_dir.iterdir():
            if item.is_dir() and not item.name.startswith('_'):
                languages.append(item.name)

        return languages


def find_renpy_sdk(search_dir: str = None) -> Optional[str]:
    """便捷函数：查找 Ren'Py SDK"""
    manager = SDKManager()
    sdk_path = manager.find_sdk(search_dir)
    return str(sdk_path) if sdk_path else None
