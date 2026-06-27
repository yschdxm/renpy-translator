"""项目管理器 - 使用 SQLite 存储项目数据

每个项目一个独立的 .db 文件，替代原来的 JSON 文件存储。
支持导入旧格式 JSON 项目（向后兼容）。
"""

import json
import os
import shutil
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

from database import ProjectDatabase


@dataclass
class ProjectInfo:
    """项目摘要信息（用于列表显示）"""
    name: str
    game_dir: str
    model_config_name: str
    total_dialogues: int = 0
    translated_dialogues: int = 0
    total_strings: int = 0
    translated_strings: int = 0
    updated_at: str = ""

    @property
    def progress_percent(self) -> float:
        total = self.total_dialogues + self.total_strings
        translated = self.translated_dialogues + self.translated_strings
        if total == 0:
            return 0
        return round(translated / total * 100, 1)

    @property
    def progress_text(self) -> str:
        return (f"{self.translated_dialogues}/{self.total_dialogues} 对话, "
                f"{self.translated_strings}/{self.total_strings} 字符串 "
                f"({self.progress_percent}%)")


class ProjectManager:
    """项目管理器

    每个项目对应 projects/<name>/project.db 文件。
    """

    def __init__(self, projects_dir: str = None):
        if projects_dir is None:
            projects_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "projects")

        self.projects_dir = Path(projects_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def _get_project_dir(self, name: str) -> Path:
        """获取项目目录路径"""
        safe_name = "".join(c for c in name if c.isalnum() or c in "._- ")
        safe_name = safe_name.strip()
        return self.projects_dir / safe_name

    def _get_db_path(self, name: str) -> Path:
        """获取项目数据库文件路径"""
        return self._get_project_dir(name) / "project.db"

    def list_projects(self) -> List[ProjectInfo]:
        """列出所有项目"""
        projects = []

        for item in self.projects_dir.iterdir():
            if not item.is_dir():
                continue

            # 查找 SQLite 数据库
            db_path = item / "project.db"
            if not db_path.exists():
                continue

            try:
                db = ProjectDatabase(str(db_path))
                db.connect()
                meta = db.get_all_meta()
                dialogue_stats = db.get_dialogue_count()
                ui_stats = db.get_ui_text_count()
                db.close()

                projects.append(ProjectInfo(
                    name=meta.get("name", item.name),
                    game_dir=meta.get("game_dir", ""),
                    model_config_name=meta.get("model_config_name", ""),
                    total_dialogues=dialogue_stats["total"],
                    translated_dialogues=dialogue_stats["translated"],
                    total_strings=ui_stats["total"],
                    translated_strings=ui_stats["translated"],
                    updated_at=meta.get("updated_at", "")
                ))
            except Exception as e:
                print(f"读取项目 {item.name} 失败: {e}")

        # 按更新时间排序
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        return projects

    def create_project(self, name: str, game_dir: str,
                       model_config_name: str = "") -> ProjectDatabase:
        """创建新项目

        Returns:
            已连接的 ProjectDatabase 实例
        """
        db_path = self._get_db_path(name)
        if db_path.exists():
            raise ValueError(f"项目 '{name}' 已存在")

        now = datetime.now().isoformat()

        # 创建数据库
        db = ProjectDatabase(str(db_path))
        db.connect()

        # 写入元数据
        db.set_meta("name", name)
        db.set_meta("game_dir", game_dir)
        db.set_meta("model_config_name", model_config_name)
        db.set_meta("created_at", now)
        db.set_meta("updated_at", now)
        db.set_meta("last_position", json.dumps({"index": 0, "file": "", "line": 0}))

        return db

    def open_project(self, name: str) -> Optional[ProjectDatabase]:
        """打开项目（返回已连接的数据库实例）"""
        db_path = self._get_db_path(name)

        if not db_path.exists():
            return None

        db = ProjectDatabase(str(db_path))
        db.connect()
        return db

    def delete_project(self, name: str) -> bool:
        """删除项目"""
        try:
            project_dir = self._get_project_dir(name)
            if project_dir.exists():
                shutil.rmtree(project_dir)
            return True
        except Exception as e:
            print(f"删除项目失败: {e}")
            return False

    def project_exists(self, name: str) -> bool:
        """检查项目是否存在"""
        return self._get_db_path(name).exists()

    def export_project_json(self, name: str) -> Optional[dict]:
        """导出项目为 JSON 字典（用于项目包导出）"""
        db = self.open_project(name)
        if not db:
            return None
        try:
            return db.to_json_dict()
        finally:
            db.close()

    def import_from_zip(self, zip_extract_dir: str,
                        project_name: str = None) -> dict:
        """从解压的 ZIP 目录导入项目（仅支持新格式 .db）

        Args:
            zip_extract_dir: ZIP 解压后的临时目录
            project_name: 项目名称（可选）

        Returns:
            {'success': bool, 'message': str, 'project_name': str}
        """
        import shutil
        extract_path = Path(zip_extract_dir)

        # 查找 project.db（新格式）
        db_file = None
        for candidate in [extract_path / "project.db"] + list(extract_path.rglob("project.db")):
            if candidate.exists():
                db_file = candidate
                break

        if not db_file:
            return {'success': False, 'message': '找不到 project.db（仅支持新格式项目包）'}

        try:
            if not project_name:
                # 从 db 读取名称
                temp_db = ProjectDatabase(str(db_file))
                temp_db.connect()
                project_name = temp_db.get_meta("name", "imported_project")
                temp_db.close()

            # 检查是否已存在
            if self.project_exists(project_name):
                i = 1
                while self.project_exists(f"{project_name}_{i}"):
                    i += 1
                project_name = f"{project_name}_{i}"

            # 复制数据库文件
            project_dir = self._get_project_dir(project_name)
            project_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_file, project_dir / "project.db")
            game_src = extract_path / "game"
            if game_src.exists():
                shutil.copytree(game_src, project_dir / "game")

            # 复制游戏文件
            game_src = extract_path / "game"
            if game_src.exists():
                shutil.copytree(game_src, project_dir / "game")

            # 复制字体
            fonts_src = extract_path / "fonts"
            if fonts_src.exists():
                shutil.copytree(fonts_src, project_dir / "fonts")

            return {
                'success': True,
                'message': f'项目已导入: {project_name}',
                'project_name': project_name
            }

        except Exception as e:
            return {'success': False, 'message': f'导入失败: {str(e)}'}
