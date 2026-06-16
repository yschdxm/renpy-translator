# 🎮 Ren'Py游戏翻译工具

一个基于Gradio的Ren'Py视觉小说游戏翻译工具，支持AI翻译和手动翻译。

## ✨ 功能特点

- 🔍 **智能解析** - 自动提取游戏对话和角色信息
- 🤖 **AI翻译** - 支持OpenAI兼容接口（GPT、Claude等）
- ✏️ **手动翻译** - 逐句翻译，完全掌控翻译质量
- 👤 **人名词典** - 确保角色名翻译一致性
- 📊 **进度跟踪** - 实时显示翻译进度
- 💾 **一键合并** - 翻译完成后合并到游戏文件

## 🚀 快速开始

### 1. 安装依赖

```bash
# 使用uv（推荐）
uv sync

# 或使用pip
pip install gradio openai
```

### 2. 启动工具

```bash
python run.py
```

访问 http://localhost:7860 打开界面

### 3. 使用步骤

1. **配置翻译器**
   - 填写API地址（如 https://api.openai.com/v1）
   - 填写API Key
   - 选择模型（如 gpt-4、claude-3 等）

2. **加载游戏**
   - 输入Ren'Py游戏目录路径
   - 点击"加载游戏"

3. **翻译对话**
   - 使用翻译工作台逐句翻译
   - 可选择AI翻译或手动翻译
   - 或使用批量翻译功能

4. **管理人名词典**
   - 编辑人名词典确保翻译一致
   - 格式：`英文名 → 中文名`

5. **导出合并**
   - 导出翻译报告
   - 合并翻译到游戏文件（自动备份）

## 📁 项目结构

```
renpy-translator/
├── src/
│   ├── main.py           # 主界面
│   ├── renpy_parser.py   # Ren'Py解析器
│   └── translator.py     # AI翻译器
├── run.py                # 启动脚本
├── pyproject.toml        # 项目配置
└── README.md             # 说明文档
```

## 🔧 配置说明

### 支持的AI接口

工具支持所有OpenAI兼容的API接口：

- OpenAI (gpt-3.5-turbo, gpt-4, etc.)
- Claude (通过OpenAI兼容接口)
- 本地模型 (如 Ollama, vLLM, etc.)
- 其他兼容接口

### 翻译参数

- **Temperature**: 控制翻译创造性（0-2，推荐0.3）
- **Max Tokens**: 最大输出长度
- **Context Lines**: 上下文行数（提高翻译连贯性）

## 📝 人名词典格式

```
Eileen → 艾琳
Lucy → 露西
John → 约翰
```

## ⚠️ 注意事项

1. **版权问题** - 请确保有游戏汉化授权
2. **备份文件** - 翻译前建议备份游戏文件
3. **翻译质量** - 建议逐句检查AI翻译结果
4. **人名词典** - 完善词典可提高翻译一致性

## 🤝 贡献

欢迎提交Issue和Pull Request！

## 📄 许可证

MIT License
