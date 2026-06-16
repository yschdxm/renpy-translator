"""配置管理器 - 负责配置的持久化存储"""

import json
import os
import base64
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import asdict, dataclass


@dataclass
class ModelConfig:
    """单个模型配置"""
    name: str  # 配置名称，如 "GPT-4", "Claude"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-3.5-turbo"
    temperature: float = 0.3
    max_tokens: int = 1000
    context_lines: int = 3
    timeout: int = 30
    max_context: int = 8  # 模型最大上下文（单位K）


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_dir: str = None):
        # 默认配置目录
        if config_dir is None:
            config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")

        self.config_dir = Path(config_dir)
        self.config_file = self.config_dir / "models.json"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # 当前选中的配置名称
        self.active_config_name: str = ""

    def _encode_api_key(self, api_key: str) -> str:
        """简单编码API Key（Base64）"""
        if not api_key:
            return ""
        return base64.b64encode(api_key.encode("utf-8")).decode("utf-8")

    def _decode_api_key(self, encoded_key: str) -> str:
        """解码API Key"""
        if not encoded_key:
            return ""
        try:
            return base64.b64decode(encoded_key.encode("utf-8")).decode("utf-8")
        except Exception:
            return encoded_key

    def load_all_configs(self) -> List[ModelConfig]:
        """加载所有模型配置"""
        if not self.config_file.exists():
            return []

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            configs = []
            for item in data.get("models", []):
                # 解码API Key
                if "api_key_encoded" in item:
                    item["api_key"] = self._decode_api_key(item["api_key_encoded"])
                item.pop("api_key_encoded", None)
                configs.append(ModelConfig(**item))

            self.active_config_name = data.get("active", "")
            return configs

        except Exception as e:
            print(f"加载配置失败: {e}")
            return []

    def save_all_configs(self, configs: List[ModelConfig], active_name: str = "") -> bool:
        """保存所有模型配置"""
        try:
            models_data = []
            for config in configs:
                item = asdict(config)
                # 编码API Key
                if item.get("api_key"):
                    item["api_key_encoded"] = self._encode_api_key(item["api_key"])
                    item["api_key"] = "***"
                models_data.append(item)

            data = {
                "models": models_data,
                "active": active_name or self.active_config_name
            }

            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            return True

        except Exception as e:
            print(f"保存配置失败: {e}")
            return False

    def add_config(self, config: ModelConfig) -> bool:
        """添加新配置"""
        configs = self.load_all_configs()

        # 检查是否重名
        for c in configs:
            if c.name == config.name:
                return False

        configs.append(config)
        return self.save_all_configs(configs)

    def update_config(self, name: str, config: ModelConfig) -> bool:
        """更新指定配置"""
        configs = self.load_all_configs()

        for i, c in enumerate(configs):
            if c.name == name:
                configs[i] = config
                return self.save_all_configs(configs)

        return False

    def delete_config(self, name: str) -> bool:
        """删除指定配置"""
        configs = self.load_all_configs()
        configs = [c for c in configs if c.name != name]
        return self.save_all_configs(configs)

    def get_config_by_name(self, name: str) -> Optional[ModelConfig]:
        """根据名称获取配置"""
        configs = self.load_all_configs()
        for c in configs:
            if c.name == name:
                return c
        return None

    def get_config_names(self) -> List[str]:
        """获取所有配置名称"""
        configs = self.load_all_configs()
        return [c.name for c in configs]

    def set_active(self, name: str) -> bool:
        """设置当前活跃的配置"""
        configs = self.load_all_configs()
        return self.save_all_configs(configs, active_name=name)
