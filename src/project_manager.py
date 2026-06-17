"""项目管理器 - 负责翻译项目的持久化存储"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict, field


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
        return f"{self.translated_dialogues}/{self.total_dialogues} 对话, {self.translated_strings}/{self.total_strings} 字符串 ({self.progress_percent}%)"


@dataclass
class Project:
    """完整的项目数据"""
    name: str
    game_dir: str
    model_config_name: str = ""
    work_dir: str = ""  # 工作目录路径
    dialogues: List[Dict[str, Any]] = field(default_factory=list)
    ui_texts: List[Dict[str, Any]] = field(default_factory=list)
    characters: List[Dict[str, Any]] = field(default_factory=list)
    char_dict: Dict[str, str] = field(default_factory=dict)
    last_position: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def get_stats(self) -> Dict[str, int]:
        """获取统计信息"""
        translated_dialogues = sum(1 for d in self.dialogues if d.get('is_translated', False))
        translated_strings = sum(1 for u in self.ui_texts if u.get('is_translated', False))
        return {
            "total_dialogues": len(self.dialogues),
            "translated_dialogues": translated_dialogues,
            "total_strings": len(self.ui_texts),
            "translated_strings": translated_strings,
            "total_characters": len(self.characters)
        }


class ProjectManager:
    """项目管理器"""

    def __init__(self, projects_dir: str = None):
        if projects_dir is None:
            # 默认在工具目录下创建 projects 文件夹
            projects_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "projects")

        self.projects_dir = Path(projects_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def _get_project_dir(self, name: str) -> Path:
        """获取项目目录路径"""
        # 清理项目名称，用作目录名
        safe_name = "".join(c for c in name if c.isalnum() or c in "._- ")
        safe_name = safe_name.strip()
        return self.projects_dir / safe_name

    def _get_project_file(self, name: str) -> Path:
        """获取项目文件路径"""
        return self._get_project_dir(name) / "project.json"

    def list_projects(self) -> List[ProjectInfo]:
        """列出所有项目"""
        projects = []

        for item in self.projects_dir.iterdir():
            if item.is_dir():
                project_file = item / "project.json"
                if project_file.exists():
                    try:
                        with open(project_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)

                        stats = data.get('stats', {})
                        projects.append(ProjectInfo(
                            name=data.get('name', item.name),
                            game_dir=data.get('game_dir', ''),
                            model_config_name=data.get('model_config_name', ''),
                            total_dialogues=stats.get('total_dialogues', 0),
                            translated_dialogues=stats.get('translated_dialogues', 0),
                            total_strings=stats.get('total_strings', 0),
                            translated_strings=stats.get('translated_strings', 0),
                            updated_at=data.get('updated_at', '')
                        ))
                    except Exception as e:
                        print(f"读取项目 {item.name} 失败: {e}")

        # 按更新时间排序
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        return projects

    def create_project(self, name: str, game_dir: str, model_config_name: str = "") -> Project:
        """创建新项目"""
        # 检查项目是否已存在
        if self._get_project_file(name).exists():
            raise ValueError(f"项目 '{name}' 已存在")

        now = datetime.now().isoformat()

        project = Project(
            name=name,
            game_dir=game_dir,
            model_config_name=model_config_name,
            created_at=now,
            updated_at=now,
            last_position={"index": 0, "file": "", "line": 0}
        )

        # 保存项目
        self.save_project(project)
        return project

    def load_project(self, name: str) -> Optional[Project]:
        """加载项目"""
        project_file = self._get_project_file(name)

        if not project_file.exists():
            return None

        try:
            with open(project_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            return Project(
                name=data.get('name', ''),
                game_dir=data.get('game_dir', ''),
                model_config_name=data.get('model_config_name', ''),
                dialogues=data.get('dialogues', []),
                ui_texts=data.get('ui_texts', []),
                characters=data.get('characters', []),
                char_dict=data.get('char_dict', {}),
                last_position=data.get('last_position', {}),
                created_at=data.get('created_at', ''),
                updated_at=data.get('updated_at', '')
            )

        except Exception as e:
            print(f"加载项目失败: {e}")
            return None

    def save_project(self, project: Project) -> bool:
        """保存项目"""
        try:
            project_dir = self._get_project_dir(project.name)
            project_dir.mkdir(parents=True, exist_ok=True)

            # 更新统计信息
            stats = project.get_stats()

            # 更新时间
            project.updated_at = datetime.now().isoformat()

            # 构建保存数据
            data = {
                "name": project.name,
                "game_dir": project.game_dir,
                "model_config_name": project.model_config_name,
                "dialogues": project.dialogues,
                "ui_texts": project.ui_texts,
                "characters": project.characters,
                "char_dict": project.char_dict,
                "last_position": project.last_position,
                "stats": stats,
                "created_at": project.created_at,
                "updated_at": project.updated_at
            }

            # 写入文件
            project_file = project_dir / "project.json"
            with open(project_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            return True

        except Exception as e:
            print(f"保存项目失败: {e}")
            return False

    def delete_project(self, name: str, delete_files: bool = False) -> bool:
        """删除项目"""
        try:
            project_dir = self._get_project_dir(name)

            if delete_files:
                # 删除整个项目目录
                import shutil
                shutil.rmtree(project_dir)
            else:
                # 只删除项目文件
                project_file = project_dir / "project.json"
                if project_file.exists():
                    os.remove(project_file)

            return True

        except Exception as e:
            print(f"删除项目失败: {e}")
            return False

    def project_exists(self, name: str) -> bool:
        """检查项目是否存在"""
        return self._get_project_file(name).exists()

    def get_project_info(self, name: str) -> Optional[ProjectInfo]:
        """获取项目摘要信息"""
        project_file = self._get_project_file(name)

        if not project_file.exists():
            return None

        try:
            with open(project_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            stats = data.get('stats', {})
            return ProjectInfo(
                name=data.get('name', ''),
                game_dir=data.get('game_dir', ''),
                model_config_name=data.get('model_config_name', ''),
                total_dialogues=stats.get('total_dialogues', 0),
                translated_dialogues=stats.get('translated_dialogues', 0),
                total_strings=stats.get('total_strings', 0),
                translated_strings=stats.get('translated_strings', 0),
                updated_at=data.get('updated_at', '')
            )

        except Exception as e:
            print(f"获取项目信息失败: {e}")
            return None
