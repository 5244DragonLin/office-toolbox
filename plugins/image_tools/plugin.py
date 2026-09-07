"""图片工具插件：将 scripts/ 下的 image_engine.py 包装为统一的插件动作。

本文件是壳与图片引擎之间的"翻译层"，核心职责是「入口归一化」：
单张 / 多张 / 文件夹 / 压缩包四种输入，最终都收敛为一个图片路径清单，
处理引擎（image_engine.py）只面对清单，完全不知道来源是什么。

- 每个动作函数签名统一为 fn(files, params, workdir) -> list[dict]
- files:  {"files": [已保存的上传文件路径, ...]}
- params: 前端表单提交的参数 dict（含壳注入的 _progress 回调）
- workdir: 本次任务专属临时目录，输出文件应写在这里
- 返回值: [{"path": 输出路径, ...引擎附加信息}]，壳会搬移到下载区并透传附加信息
"""
import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# 首次 import 失败时，壳会自动按 requirements.txt 安装缺失依赖并重试
import image_engine  # noqa: E402

# 支持直接解开的压缩包格式。zip 用标准库零依赖；7z/rar 属可选依赖组
# （requirements-archive.txt），rar 还需系统安装 unrar，故不放进 manifest 默认 accept
ARCHIVE_EXTS = {".zip", ".7z", ".rar"}

# 解压炸弹防护：单包解压总量 / 条目数上限
MAX_UNPACK_TOTAL = 2 * 1024 * 1024 * 1024  # 2GB
MAX_UNPACK_ENTRIES = 10000

# 输出文件超过该数量时额外提供一个打包 zip，逐个勾选下载太繁琐
ZIP_WHEN_OVER = 20


# ---------- 归一化入口 ----------

def _extract_zip(src: Path, dest: Path) -> None:
    """解压 zip（标准库）。带解压炸弹防护与嵌套压缩包跳过。"""
    total, count = 0, 0
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            count += 1
            total += info.file_size
            if count > MAX_UNPACK_ENTRIES:
                raise ValueError(f"压缩包条目数超过上限 {MAX_UNPACK_ENTRIES}，已中止解压")
            if total > MAX_UNPACK_TOTAL:
                raise ValueError("压缩包解压总量超过 2GB 上限，已中止解压")
            # member 名来自客户端不可信：滤掉 .. / 盘符 / 绝对路径成分，防 zip slip
            safe_parts = [p for p in Path(info.filename).parts
                          if p not in ("..", "/", "\\") and ":" not in p and p not in (".", "")]
            if not safe_parts:
                continue
            target = dest.joinpath(*safe_parts)
            if target.suffix.lower() in ARCHIVE_EXTS:
                continue  # 嵌套压缩包不递归解开，也不当作图片
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as fsrc, open(target, "wb") as fdst:
                fdst.write(fsrc.read())


def _extract_generic(src: Path, dest: Path) -> None:
    """解压 7z / rar（可选依赖组，缺失时给出可执行的指引）。"""
    try:
        if src.suffix.lower() == ".7z":
            import py7zr
            with py7zr.SevenZipFile(src) as zf:
                zf.extractall(dest)
        else:
            import rarfile
            with rarfile.RarFile(src) as rf:
                rf.extractall(dest)
    except ImportError:
        raise ValueError(
            f"解压 {src.suffix} 需要可选依赖，请执行 "
            f"pip install -r plugins/image_tools/requirements-archive.txt"
            f"（rar 还需系统安装 unrar 并加入 PATH）"
        )


def _collect_images(paths: list[Path]) -> list[Path]:
    """递归收集路径清单中的所有图片文件，按文件名排序保证结果稳定。"""
    found: set[Path] = set()
    for p in paths:
        if p.is_dir():
            found.update(f for f in p.rglob("*")
                         if f.is_file() and f.suffix.lower() in image_engine.IMAGE_EXTS)
        elif p.suffix.lower() in image_engine.IMAGE_EXTS:
            found.add(p)
    return sorted(found, key=lambda f: str(f).lower())


def _normalize_inputs(file_paths: list[str], workdir: Path) -> list[Path]:
    """把上传清单展开为图片路径清单：压缩包解开，文件夹递归扫描，杂项跳过。

    展开同时保留压缩包内的相对目录（输出侧按原结构回放）；散装上传文件由壳
    已拍平到 workdir 根，输出按拍平后的名字命名。
    """
    image_sources: list[Path] = []
    for i, raw in enumerate(file_paths):
        src = Path(raw)
        if src.suffix.lower() in ARCHIVE_EXTS:
            dest = workdir / "_unpacked" / f"{i:03d}"
            dest.mkdir(parents=True, exist_ok=True)
            if src.suffix.lower() == ".zip":
                _extract_zip(src, dest)
            else:
                _extract_generic(src, dest)
            image_sources.append(dest)
        elif src.is_file():
            image_sources.append(src)
    images = _collect_images(image_sources)
    if not images:
        raise ValueError(
            "未在输入中找到可用图片（支持 jpg/png/webp/bmp/tif/gif）；"
            "传压缩包时请确认里面直接包含图片文件（嵌套压缩包不会解开）"
        )
    return images


def _dedupe_out_name(used: set[str], stem: str, ext: str) -> str:
    """为输出文件名去重：撞名时追加序号（批量输入里常有同名图）。"""
    name = f"{stem}{ext}"
    idx = 2
    while name in used:
        name = f"{stem}_{idx}{ext}"
        idx += 1
    used.add(name)
    return name


def _batch(file_paths: list[str], params: dict, workdir: Path, engine_fn) -> list[dict]:
    """批量动作的公共骨架：归一化 -> 逐张调引擎 -> 汇总 -> 超量打包。"""
    images = _normalize_inputs(file_paths, workdir)
    out_root = workdir / "output"
    progress = params.get("_progress")
    used_names: set[str] = set()
    results, failures = [], []

    for i, src in enumerate(images, 1):
        if progress:
            progress(message=f"第 {i}/{len(images)} 张：{src.name}")
        # 压缩包输入按包内相对路径回放目录结构（首段是展开用的序号目录，剥掉）；
        # 散装输入输出在根目录
        try:
            rel_parts = src.relative_to(workdir / "_unpacked").parts
            rel_dir = Path(*rel_parts[1:-1]) if len(rel_parts) > 2 else Path()
        except ValueError:
            rel_dir = Path()
        sub_out = out_root / rel_dir
        try:
            info = engine_fn(src, params, sub_out)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{src.name}: {exc}")
            continue
        out_path = Path(info["path"])
        name = _dedupe_out_name(used_names, out_path.stem, out_path.suffix)
        final = sub_out / name
        if out_path != final:
            out_path.rename(final)
        entry = {"path": str(final)}
        entry.update({k: v for k, v in info.items() if k != "path"})
        results.append(entry)

    if failures:
        if not results:
            raise ValueError("全部图片处理失败：" + "；".join(failures[:5]))
        # 部分失败时生成报告文件随结果一起返回（壳会忽略无 path 的条目，
        # 失败清单必须落成文件才能到达用户眼前）
        report = out_root / "处理报告.txt"
        report.write_text("以下图片处理失败，已跳过：\n" + "\n".join(failures),
                          encoding="utf-8")
        results.append({"path": str(report), "note": f"⚠️ {len(failures)} 张失败，详见报告"})

    # 超过阈值时追加一个整包 zip，输出树按原相对目录结构打包
    outputs = [r for r in results if r["path"]]
    if len(outputs) > ZIP_WHEN_OVER:
        zip_path = workdir / f"output_{len(outputs)}张_打包.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(out_root.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(out_root))
        results.insert(0, {"path": str(zip_path), "note": f"整包下载（含全部 {len(outputs)} 张）"})
    return results


# ---------- 动作包装（每个动作一个函数，业务全部委托引擎） ----------

def act_resize_image(files, params, workdir: Path):
    """分辨率调整：支持 单张/多张/文件夹/压缩包 输入，保持宽高比。"""
    return _batch(files.get("files") or [], params, workdir, image_engine.resize_image)


def act_compress_image(files, params, workdir: Path):
    """图片压缩：按质量或目标体积压缩，可选叠加缩放。"""
    return _batch(files.get("files") or [], params, workdir, image_engine.compress_image)


def act_enhance_image(files, params, workdir: Path):
    """图片增强：放大 / 锐化 / 对比度 / 降噪，可叠加。"""
    return _batch(files.get("files") or [], params, workdir, image_engine.enhance_image)


ACTIONS = {
    "resize_image": act_resize_image,
    "compress_image": act_compress_image,
    "enhance_image": act_enhance_image,
}
