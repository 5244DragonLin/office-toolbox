# office-toolbox：本地个人办公工具箱

基于 FastAPI 的本地个人办公工具箱。采用「壳 + 插件」架构：壳永远轻量，工具按需安装、代码按需加载、依赖按需引入，装多少工具都不会臃肿。开箱内置电子书制作、MD转公文Word、抖音下载、音频切分工具四个插件域，后续工具即插即用。

## 📸项目预览

![首页](assets/homepage.png)

## 为什么需要这个工具？

- 常见格式转换（TXT 转 EPUB、Markdown 转 Word…）要装一堆软件，每个软件只干一件事，还互不通用
- 在线转换网站需要上传隐私文档，有大小限制、有广告，转换质量参差不齐
- 工具越攒越多，每套都有自己的界面和用法，维护成本越来越高

**office-toolbox 解决这些问题**：一个壳统一管理所有工具，工具以插件形式即插即用，用哪个装哪个，不用的随时抽走。

## ⭐亮点

- **壳 + 插件架构**：壳只做三件事（启动、卡片首页、插件注册），永远不长胖
- **按需加载与安装**：插件代码首次使用时才加载、依赖才自动 pip 安装（缺什么装什么），没点开的工具零内存、零启动开销
- **零前端开发**：工具界面由壳根据 `manifest.json` 自动生成，新插件只需写一个 Python 文件
- **一个文件夹一个工具**：装 = 拷贝文件夹，卸 = 删文件夹，互不影响
- **本地优先**：数据只在本机处理，不上传任何第三方服务器
- **环境零污染**：所有依赖装在项目自带的 `.venv` 虚拟环境里，不影响本机 Python；卸载 = 删掉项目文件夹，不留任何残留
- **用完即清（下载即焚）**：生成结果仅在下载时短暂留存于本机，取走后本地副本立即删除、不做长期存档；临时文件 30 分钟兜底过期，磁盘不悄悄膨胀

## 📊能力矩阵

| 工具 | 输入 | 输出 | 能力亮点 |
|------|------|------|---------|
| 📚 电子书制作 | TXT / Markdown / EPUB | .md / .epub | TXT↔Markdown↔EPUB 互转，自动识别章节标题（9 种格式），支持封面与完整元数据 |
| 📄 MD转公文Word | .md / .markdown | .docx | 公文排版：宋体/黑体/楷体/仿宋多级标题、28pt 固定行距、三线表、自动匹配中文公文序号 |
| 🎵 抖音下载 | 网页链接 / 短链 / 纯数字 ID | MP4 / m4a / mp3 / flac | 下载无水印视频；提取原始音轨（m4a 无损搬运，音质与源一致） |
| 🎧 音频切分工具 | mp3 / wav / flac / m4a / aac / ogg / wma / opus / aiff | 分段音频（原格式或 mp3/wav/flac/m4a） | 按时间点 / 等长片段切分；保持原格式为无损流拷贝（速度快），转码输出时间轴更精确 |

## 🚀快速开始

### 1. 克隆项目

```bash
# Gitee 镜像（国内访问快）
git clone https://gitee.com/yhl5244/office-toolbox.git

# GitHub 原仓库
git clone https://github.com/5244DragonLin/office-toolbox.git

cd office-toolbox
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> 只需安装壳依赖。各插件的依赖在**首次使用该插件时自动安装**，也可手动执行 `pip install -r plugins/<插件id>/requirements.txt` 预装。

### 3. 运行

Windows 双击 `start.bat`（自动创建虚拟环境 + 安装依赖 + 启动 + 打开浏览器）：

```bash
python -m src.main
```

启动后浏览器自动打开 `http://127.0.0.1:8765`，首页即插件卡片墙。若浏览器未自动打开，手动访问该地址即可。

## 🧩插件开发

新增一个工具只需两步：建一个插件文件夹，写两个文件。

### 目录约定

```text
plugins/<插件id>/
├── manifest.json      # 插件声明（名称 / 图标 / 子功能列表）
├── plugin.py          # 壳适配层：解包上传文件、传参、返回结果路径（每个动作一个函数）
├── requirements.txt   # 本插件的依赖清单（按需安装）
└── scripts/           # 可选：核心业务逻辑（转换/处理引擎），与壳解耦、可独立测试复用
```

> **plugin.py 和 scripts/ 的分工**：`plugin.py` 是壳与业务之间的翻译层，只负责适配壳协议（接收 `files`/`params`/`workdir`，返回输出路径），保持轻薄；真正的转换/处理逻辑放在 `scripts/` 下，写成纯函数（输入文件路径 + 参数 → 输出路径），不依赖壳的任何约定，可脱离 Web 服务单独测试、被其他插件复用。简单插件（逻辑很少）也可全部写在 `plugin.py` 里，无需强行拆目录。

### manifest.json

```json
{
  "id": "my_tool",
  "name": "我的工具",
  "version": "0.1.0",
  "description": "一句话说明这个插件做什么",
  "icon": "🛠️",
  "actions": [
    {
      "id": "do_something",
      "name": "执行某转换",
      "description": "子功能说明",
      "input": "file",
      "accept": ".txt,.md",
      "params": [
        { "name": "title", "label": "标题", "type": "text" }
      ]
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `input` | `file`（单文件）或 `files`（多文件） |
| `accept` | 允许的文件扩展名，逗号分隔 |
| `params` | 表单参数声明，`type` 支持 `text` / `number` |

### plugin.py

```python
ACTIONS = {
    "do_something": handle_do_something,
}

def handle_do_something(files, params, workdir):
    """files: {"files": [上传文件路径,...]}  params: 表单参数  workdir: 本次任务目录
    返回输出文件路径列表。"""
    src = files["files"][0]
    out = workdir / "result.md"
    # ... 你的转换逻辑
    return [out]
```

放好文件后刷新首页，新插件自动出现在卡片墙，无需改壳的任何代码。

## 🗂️项目结构

```text
office-toolbox/
├── src/                      # 壳（永远轻量）
│   ├── main.py               # 服务入口：FastAPI + 插件分发 + 下载
│   ├── registry.py           # 插件注册表：扫描 / 按需加载 / 依赖按需安装
│   └── web/
│       └── index.html        # 卡片墙 + 统一工具面板（单页）
├── plugins/                  # 插件目录（一个文件夹 = 一个工具域）
│   ├── ebook_maker/          # 电子书制作域：TXT/Markdown/EPUB 互转
│   │   ├── manifest.json
│   │   ├── plugin.py
│   │   └── scripts/          # 转换脚本（txt_to_markdown / markdown_to_epub 等）
│   ├── md_to_word/           # MD转公文Word域：Markdown 转公文排版 Word
│   │   ├── manifest.json
│   │   ├── plugin.py
│   │   └── scripts/
│   └── douyin_download/      # 抖音下载域：视频 / 音频下载（按链接或 ID）
│       ├── manifest.json
│       ├── plugin.py
│       └── scripts/          # 抓取脚本（链接解析 / ABogus 签名 / 下载 / 抽音轨）
│   └── audio_tools/          # 音频切分工具域：按时间点 / 等长片段切分音频
│       ├── manifest.json
│       ├── plugin.py
│       └── scripts/          # 切分脚本（audio_splitter：切分点解析 / ffmpeg 调用）
├── assets/                   # README 图片
├── output/                   # 运行产物（下载区 / 临时目录，已 gitignore）
├── requirements.txt          # 壳依赖
├── start.bat / start.sh      # 一键启动
└── README.md
```

## ❓️FAQ

**如何添加一个新工具？**

复制 `plugins/` 下任一插件的结构，新建插件文件夹并填写 `manifest.json` 和 `plugin.py`，刷新首页即可。详见「插件开发」章节。

**插件依赖会自动安装吗？**

会。首次使用某插件时，壳检测到缺少依赖会自动执行 `pip install -r` 该插件的 `requirements.txt`。也可以在运行前手动预装。

**会不会越用越臃肿？**

不会。壳不包含任何工具功能；插件代码按需加载（首次使用才 import）；依赖按需安装。删掉不用的插件文件夹即彻底移除。

**文件会被上传到网络吗？**

不会。服务只监听 `127.0.0.1`（本机），所有转换都在本地完成，不上传任何第三方服务器。

## 📝已知问题 / 待改进点

- [ ] 更多插件域（主要是个人工作中遇到的、可提升生产力的插件）
- [ ] 插件依赖的独立虚拟环境隔离，避免插件间依赖冲突
- [ ] 深色模式与统一图标库

## 🤝贡献

欢迎提 Issue 和 PR！

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/your-feature`
3. 提交改动：保持提交粒度清晰
4. 推送并提交 Pull Request

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 📋更新日志

### v0.1.1

- **新增：** 电子书制作插件「EPUB → Markdown」转换结果除 .md 正文外，同时提供书籍封面图片下载（结果列表以「🖼 封面」与「📄 正文」区分，可分别勾选下载）
- **优化：** 电子书制作插件「EPUB → Markdown」「TXT → Markdown」均支持一次多选多本、批量转换（每本各输出一个 .md，相同文件名自动追加序号防覆盖）
- **优化：** 电子书制作插件「EPUB → Markdown」转换质量增强——①识别 Calibre 等导出的 `<p id="toc-anchor">` 式伪标题并升级为二级标题；②清理版权页/推广语/对话开头等误识别标题；③统一标题层级为正文固定「# 书名」+ 多个「## 章节」
- **修复：** 下载转换结果时文件名带任务ID前缀，现改为下载时自动还原为干净的原始文件名；磁盘上仍保留任务ID前缀，防止多任务同名文件互相覆盖

### v0.1

- 首个测试版本：壳 + 插件架构落地（FastAPI 本地服务 + 卡片首页 + 插件注册表）
- 首批内置四个插件域：电子书制作（TXT/Markdown/EPUB 互转）、MD转公文Word、抖音下载（无水印视频 + 提取音轨）、音频切分工具（按时间点 / 等长片段）

## 🙏致谢

本项目在开发过程中参考、借鉴或复用了以下开源项目，特此致谢：

- [**douyin-downloader**](https://github.com/jiji262/douyin-downloader) — 抖音下载插件的整体抓取思路与实现参考（链接解析、详情接口调用流程）

## ☕捐赠

如果这个项目对你有帮助，欢迎请作者喝杯咖啡～

| 支付宝 | 微信 |
|--------|------|
| ![支付宝](https://gitee.com/yhl5244/images/raw/master/donate_alipay.jpg) | ![微信](https://gitee.com/yhl5244/images/raw/master/donate_wechat.jpg) |

## ⚠️免责声明

本工具仅供学习交流使用，不得用于任何违反法律法规或侵犯第三方权益的用途。
因使用本工具产生的一切后果由使用者自行承担，作者不承担任何法律责任。

## 📃许可证

本项目基于 [MIT](LICENSE) 协议开源。
