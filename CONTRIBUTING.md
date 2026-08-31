# 贡献指南

欢迎参与本项目的开发！无论是修复 Bug、新增功能、完善文档，还是贡献一个新的工具插件，都可以通过 Issue 和 Pull Request 参与进来。

## 贡献流程

1. **Fork** 本仓库到你的账户
2. **Clone** 你的 Fork 到本地：`git clone https://github.com/<your-name>/office-toolbox.git`
3. 创建**特性分支**：`git checkout -b feature/your-feature`
4. **开发**并提交：保持提交粒度清晰，信息言简意赅
5. **推送**分支：`git push origin feature/your-feature`
6. 提交 **Pull Request** 到本仓库的 `main` 分支，并在 PR 描述中说明改动内容

## 开发约定

- 遵循本仓库已有的代码风格与目录结构（见 `README.md` 与项目根目录规范）
- 提交信息使用中文或英文均可，但需清晰描述改动意图
- 新增功能建议同步更新 `README.md` 的「亮点」「快速开始」与「更新日志」
- 新增插件请遵循 `plugins/` 下的插件协议（`manifest.json` + `plugin.py`），参考已有插件实现
- 若涉及配置项变更，请同步更新 `config.example.yaml` 与 `README.md` 的「配置说明」

## 问题反馈

- 使用中发现问题，请先检索已有的 [Issues](../../issues)，避免重复提交
- 提交新 Issue 时请尽量包含：复现步骤、预期行为、实际行为、运行环境（系统 / Python 版本 / 依赖版本）

感谢你的贡献 ❤️
