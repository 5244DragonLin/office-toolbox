"""插件注册表：扫描 plugins/ 目录，解析 manifest，按需加载插件代码。

设计要点（按需三原则）：
1. 启动时只读 manifest.json（轻量电话簿），不加载任何插件代码；
2. 插件代码在首次被调用时才 import（按需加载）；
3. 插件依赖在首次使用时检查，缺失才自动 pip 安装（按需安装）。
"""
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = PROJECT_ROOT / "plugins"
OUTPUT_DIR = PROJECT_ROOT / "output"
DOWNLOADS_DIR = OUTPUT_DIR / "downloads"
TMP_DIR = OUTPUT_DIR / "tmp"


class PluginRegistry:
    """插件注册表。"""

    def __init__(self, plugins_dir=None):
        self.plugins_dir = Path(plugins_dir) if plugins_dir else PLUGINS_DIR
        self._manifests: dict[str, dict] = {}
        self._modules: dict[str, object] = {}
        self.scan()

    # ---------- 扫描与清单 ----------

    def scan(self):
        """扫描 plugins/ 下每个含 manifest.json 的文件夹，登记插件。"""
        self._manifests.clear()
        if not self.plugins_dir.exists():
            return
        for folder in sorted(self.plugins_dir.iterdir()):
            manifest_file = folder / "manifest.json"
            if not manifest_file.is_file():
                continue
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                manifest["_dir"] = str(folder)
                self._manifests[manifest["id"]] = manifest
            except Exception as exc:  # noqa: BLE001
                print(f"[registry] 插件 {folder.name} 加载失败: {exc}")

    def list_plugins(self) -> list[dict]:
        """返回首页卡片所需的插件摘要（不含插件代码，只含 manifest 信息）。

        每次调用前重扫一遍插件目录：扫描只读几个 manifest.json，成本可忽略，
        却能兑现「装 = 拷贝文件夹，刷新首页即可见，无需重启」的设计承诺。
        已加载的插件模块（self._modules）不受影响。
        """
        self.scan()
        result = []
        for pid in sorted(self._manifests):
            m = self._manifests[pid]
            result.append({
                "id": pid,
                "name": m.get("name", pid),
                "version": m.get("version", ""),
                "description": m.get("description", ""),
                "icon": m.get("icon", "tool"),
                "actions": m.get("actions", []),
                # 是否支持扫码登录（manifest 声明式标记，与插件模块是否已加载无关；
                # API 文档页据此决定是否展示该插件的登录接口）
                "login": bool(m.get("login", False)),
                "loaded": pid in self._modules,
            })
        return result

    def manifests(self) -> list[dict]:
        """返回全部 manifest 原始数据（设置页读 assets 等完整声明用）。"""
        return list(self._manifests.values())

    def get_action(self, pid: str, aid: str):
        """按插件 id + 动作 id 取 (manifest, action)，找不到返回 (None, None)。"""
        manifest = self._manifests.get(pid)
        if not manifest:
            return None, None
        for action in manifest.get("actions", []):
            if action["id"] == aid:
                return manifest, action
        return manifest, None

    # ---------- 按需加载 ----------

    def load_module(self, pid: str, progress=None):
        """按需加载插件代码：首次调用才 import，缺依赖自动安装后重试。

        progress：可选的 progress(percent, message) 回调（与插件动作共用同一
        约定）。首次 import 触发依赖自动安装时逐包上报进度，避免装大包
        （如 torch）时调用方长时间无任何反馈。
        """
        if pid in self._modules:
            return self._modules[pid]
        manifest = self._manifests.get(pid)
        if not manifest:
            raise KeyError(f"未知插件: {pid}")
        plugin_file = Path(manifest["_dir"]) / "plugin.py"
        for attempt in (1, 2):
            try:
                spec = importlib.util.spec_from_file_location(f"_plugin_{pid}", plugin_file)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                self._modules[pid] = module
                return module
            except ImportError as exc:
                if attempt == 1:
                    print(f"[registry] 插件 {pid} 依赖缺失（{exc}），正在自动安装…")
                    self._install_deps(manifest, progress)
                    continue
                raise RuntimeError(
                    f"插件 {pid} 依赖安装失败：{exc}，请手动执行 "
                    f"pip install -r {Path(manifest['_dir']) / 'requirements.txt'}"
                ) from exc

    @staticmethod
    def _install_deps(manifest: dict, progress=None):
        """安装插件 requirements.txt 中的依赖（必选组）。

        复用 deps 的逐包安装：进度逐包上报、失败带 pip 输出尾部；
        只装必选组——requirements-*.txt 可选组（如转写依赖）不在此被动安装。
        """
        from . import deps
        deps.pip_install_op(manifest["id"], None, progress)
