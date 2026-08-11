"""提示词参考数据（静态，不入库）

从 database 层拆出：这些是构建 prompt 用的静态参考翻译，
不属于数据库持久化范畴。
"""

# 常见游戏 UI 标准翻译（静态参考，不存入数据库）
UI_GLOSSARY = {
    # 菜单
    "Start Game": "开始游戏", "New Game": "新游戏", "Load Game": "读取游戏",
    "Save Game": "保存游戏", "Main Menu": "主菜单", "Options": "选项",
    "Settings": "设置", "Preferences": "偏好设置", "Quit": "退出",
    "Exit": "退出", "About": "关于", "Help": "帮助",
    # 存档
    "Save": "保存", "Load": "读取", "Delete": "删除",
    "Auto Save": "自动保存", "Quick Save": "快速保存", "Quick Load": "快速读取",
    "Save Slot": "存档位", "Save Page": "存档页", "Load Page": "读取页",
    "No saves found.": "未找到存档。", "Save your game?": "保存游戏？",
    "Load your game?": "读取游戏？",
    # 通用按钮
    "OK": "确定", "Yes": "是", "No": "否", "Cancel": "取消",
    "Back": "返回", "Next": "下一页", "Previous": "上一页",
    "Close": "关闭", "Confirm": "确认", "Apply": "应用",
    "Reset": "重置", "Default": "默认",
    # 显示设置
    "Display": "显示", "Window": "窗口", "Fullscreen": "全屏",
    "Resolution": "分辨率", "Text Speed": "文字速度",
    "Auto-Forward Time": "自动前进时间", "Skip": "跳过",
    "Unseen Text": "未读文本", "After Choices": "选项后",
    "Transitions": "转场效果",
    # 音量
    "Music Volume": "音乐音量", "Sound Volume": "音效音量",
    "Voice Volume": "语音音量", "Mute All": "全部静音",
    # 对话
    "History": "历史", "Auto": "自动", "Quick": "快速",
    "Click to continue": "点击继续", "Click to dismiss": "点击关闭",
    # 辅助功能
    "Self-voicing": "自动朗读", "Self-voicing disabled": "自动朗读已禁用",
    "Self-voicing enabled": "自动朗读已启用",
    # 其他
    "Are you sure?": "确定吗？", "Loading...": "加载中...",
    "Please wait": "请稍候", "Error": "错误", "Warning": "警告",
    "Language": "语言", "Rollback Side": "回滚方向",
    "Disable": "禁用", "Enable": "启用",
    "Left": "左", "Right": "右",
}
