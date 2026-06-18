"""项目导出导入管理器"""

import json
import os
import shutil
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any


class ProjectExporter:
    """项目导出导入管理器"""

    def __init__(self, projects_dir: str):
        self.projects_dir = Path(projects_dir)

    def _convert_to_relative(self, file_path: str, project_game_dir: Path) -> str:
        """将绝对路径转换为相对路径（使用POSIX格式）"""
        try:
            abs_path = Path(file_path)
            if abs_path.is_absolute():
                # 尝试相对于项目game目录
                rel_path = abs_path.relative_to(project_game_dir)
                return rel_path.as_posix()
        except ValueError:
            pass

        # 如果无法转换，尝试提取game之后的部分
        parts = Path(file_path).parts
        if 'game' in parts:
            idx = parts.index('game')
            return Path(*parts[idx:]).as_posix()

        # 如果都没有，返回文件名
        return Path(file_path).name

    def export_project(self, project_name: str, export_path: str) -> Dict[str, Any]:
        """导出项目为zip文件

        Args:
            project_name: 项目名称
            export_path: 导出zip文件路径

        Returns:
            {'success': bool, 'message': str}
        """
        try:
            project_dir = self.projects_dir / project_name
            if not project_dir.exists():
                return {'success': False, 'message': f'项目 {project_name} 不存在'}

            # 读取项目配置
            project_file = project_dir / 'project.json'
            with open(project_file, 'r', encoding='utf-8') as f:
                project_data = json.load(f)

            # 保存原始路径信息
            project_data['_export_info'] = {
                'exported_at': datetime.now().isoformat(),
                'original_game_dir': project_data.get('game_dir', ''),
            }

            # 项目game目录路径（用于计算相对路径）
            project_game_dir = project_dir / 'game'

            # 转换对话中的文件路径为相对路径（使用POSIX格式，兼容所有平台）
            for d in project_data.get('dialogues', []):
                fp = d.get('file_path', '')
                if fp:
                    d['file_path'] = self._convert_to_relative(fp, project_game_dir)

            # 转换字符串中的文件路径
            for u in project_data.get('ui_texts', []):
                fp = u.get('file_path', '')
                if fp:
                    u['file_path'] = self._convert_to_relative(fp, project_game_dir)

            # 转换最后位置的文件路径
            last_pos = project_data.get('last_position', {})
            if last_pos.get('file'):
                last_pos['file'] = self._convert_to_relative(last_pos['file'], project_game_dir)

            # 清除绝对路径（导入时需要重新指定）
            project_data['game_dir'] = ''
            project_data['work_dir'] = ''

            # 创建临时目录
            import tempfile
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_project_dir = Path(temp_dir) / project_name
                temp_project_dir.mkdir()

                # 保存修改后的项目配置
                with open(temp_project_dir / 'project.json', 'w', encoding='utf-8') as f:
                    json.dump(project_data, f, ensure_ascii=False, indent=2)

                # 复制游戏工作目录（如果存在）
                game_work_dir = project_dir / 'game'
                if game_work_dir.exists():
                    shutil.copytree(game_work_dir, temp_project_dir / 'game')

                # 复制字体目录（如果存在）
                fonts_dir = project_dir.parent.parent / 'fonts'
                if fonts_dir.exists():
                    shutil.copytree(fonts_dir, temp_project_dir / 'fonts')

                # 创建zip文件
                export_path = Path(export_path)
                export_path.parent.mkdir(parents=True, exist_ok=True)

                with zipfile.ZipFile(export_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for root, dirs, files in os.walk(temp_project_dir):
                        for file in files:
                            file_path = Path(root) / file
                            arcname = file_path.relative_to(temp_dir)
                            zf.write(file_path, arcname)

            return {
                'success': True,
                'message': f'项目已导出到: {export_path}'
            }

        except Exception as e:
            return {'success': False, 'message': f'导出失败: {str(e)}'}

    def import_project(self, zip_path: str,
                       project_name: str = None) -> Dict[str, Any]:
        """导入项目

        Args:
            zip_path: zip文件路径
            project_name: 项目名称（可选，默认使用原名称）

        Returns:
            {'success': bool, 'message': str, 'project_name': str}
        """
        try:
            zip_path = Path(zip_path)
            if not zip_path.exists():
                return {'success': False, 'message': 'zip文件不存在'}

            # 创建临时目录解压
            import tempfile
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_dir = Path(temp_dir)

                # 解压zip文件
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(temp_dir)

                # 查找项目目录
                extracted_dirs = [d for d in temp_dir.iterdir() if d.is_dir()]
                if not extracted_dirs:
                    return {'success': False, 'message': 'zip文件中没有找到项目'}

                project_dir = extracted_dirs[0]

                # 读取项目配置
                project_file = project_dir / 'project.json'
                if not project_file.exists():
                    return {'success': False, 'message': 'zip文件中没有project.json'}

                with open(project_file, 'r', encoding='utf-8') as f:
                    project_data = json.load(f)

                # 使用原名称或指定名称
                if not project_name:
                    project_name = project_data.get('name', 'imported_project')

                # 检查项目是否已存在
                target_dir = self.projects_dir / project_name
                if target_dir.exists():
                    # 添加后缀避免冲突
                    i = 1
                    while target_dir.exists():
                        target_dir = self.projects_dir / f"{project_name}_{i}"
                        i += 1
                    project_name = target_dir.name

                # 复制项目文件到目标目录
                target_dir.mkdir(parents=True)

                # 复制项目配置
                shutil.copy2(project_file, target_dir / 'project.json')

                # 复制游戏工作目录
                game_src = project_dir / 'game'
                if game_src.exists():
                    shutil.copytree(game_src, target_dir / 'game')

                # 复制字体目录
                fonts_src = project_dir / 'fonts'
                if fonts_src.exists():
                    shutil.copytree(fonts_src, target_dir / 'fonts')

                # 更新项目配置中的路径
                project_file_target = target_dir / 'project.json'
                with open(project_file_target, 'r', encoding='utf-8') as f:
                    project_data = json.load(f)

                # 更新项目名称
                project_data['name'] = project_name

                # 项目中的game目录就是游戏目录
                project_data['game_dir'] = str(target_dir / 'game')
                project_data['work_dir'] = ''

                # 更新对话中的文件路径（相对路径转绝对路径）
                for d in project_data.get('dialogues', []):
                    fp = d.get('file_path', '')
                    if fp and not Path(fp).is_absolute():
                        d['file_path'] = str(target_dir / 'game' / fp)

                # 更新字符串中的文件路径
                for u in project_data.get('ui_texts', []):
                    fp = u.get('file_path', '')
                    if fp and not Path(fp).is_absolute():
                        u['file_path'] = str(target_dir / 'game' / fp)

                # 更新最后位置
                last_pos = project_data.get('last_position', {})
                if last_pos.get('file'):
                    fp = last_pos['file']
                    if not Path(fp).is_absolute():
                        last_pos['file'] = str(target_dir / 'game' / fp)

                # 保存更新后的配置
                with open(project_file_target, 'w', encoding='utf-8') as f:
                    json.dump(project_data, f, ensure_ascii=False, indent=2)

                return {
                    'success': True,
                    'message': f'项目已导入: {project_name}',
                    'project_name': project_name
                }

        except Exception as e:
            return {'success': False, 'message': f'导入失败: {str(e)}'}
            if fp and not os.path.isabs(fp):
                # 将相对路径转换为绝对路径
                d['file_path'] = str(game_dir / fp)

        # 保存更新后的配置
        with open(project_file, 'w', encoding='utf-8') as f:
            json.dump(project_data, f, ensure_ascii=False, indent=2)
