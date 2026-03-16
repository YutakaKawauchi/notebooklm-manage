#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "notebooklm-py>=0.3.4",
#     "pillow",
# ]
# ///
"""NotebookLM artifact management tool.

Interactive fzf-based tool for listing, downloading, and deleting
artifacts from NotebookLM notebooks.

Usage:
    uv run manage-artifacts.py              # インタラクティブ
    uv run manage-artifacts.py -l           # リスト表示のみ
    uv run manage-artifacts.py -d           # dry-run
    uv run manage-artifacts.py -n <id>      # ノートブック直接指定
    uv run manage-artifacts.py -c 5         # 5並列実行

Dependencies: notebooklm-py, fzf (CLI)
"""

import argparse
import asyncio
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from notebooklm import NotebookLMClient

logger = logging.getLogger("manage-artifacts")

# ─────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────

def _default_backup_dir() -> str:
    """Platform-aware default backup directory."""
    if platform.system() == "Windows":
        return str(Path.home() / "Documents" / "NotebookArtifacts")
    return str(Path.home() / "NotebookArtifacts")


DEFAULT_BACKUP_DIR = _default_backup_dir()
DEFAULT_TIMEOUT = 90
DEFAULT_CONCURRENCY = 4


def _detect_ascii_mode() -> bool:
    """Detect terminals with poor emoji support (e.g. Git Bash / mintty / legacy cmd.exe)."""
    if os.environ.get("MSYSTEM"):  # Git Bash sets MINGW64, MSYS, etc.
        return True
    if os.environ.get("NO_EMOJI", "").strip() == "1":
        return True
    # Windows legacy console (not Windows Terminal / ConEmu / Cmder)
    if sys.platform == "win32" and not os.environ.get("WT_SESSION"):
        if not os.environ.get("ConEmuPID") and not os.environ.get("CMDER_ROOT"):
            return True
    return False


USE_ASCII = _detect_ascii_mode()


def _e(emoji: str, ascii_alt: str) -> str:
    """Return emoji or ASCII alternative based on terminal capability."""
    return ascii_alt if USE_ASCII else emoji


def _sep(length: int = 39) -> str:
    """Return a separator line."""
    return "-" * length if USE_ASCII else "━" * length


ARTIFACT_TYPE_MAP = {
    "audio": {"icon": "🎵", "ascii": "[A]", "ext": "mp4", "dl": "download_audio", "order": 0},
    "infographic": {"icon": "📊", "ascii": "[I]", "ext": "png", "dl": "download_infographic", "order": 1},
    "report": {"icon": "📝", "ascii": "[R]", "ext": "md", "dl": "download_report", "order": 2},
    "slide_deck": {"icon": "📑", "ascii": "[S]", "ext": "pdf", "dl": "download_slide_deck", "order": 3},
    "data_table": {"icon": "📋", "ascii": "[D]", "ext": "csv", "dl": "download_data_table", "order": 4},
    "quiz": {"icon": "❓", "ascii": "[Q]", "ext": "json", "dl": "download_quiz", "order": 5},
    "flashcards": {"icon": "🃏", "ascii": "[F]", "ext": "json", "dl": "download_flashcards", "order": 6},
    "video": {"icon": "🎬", "ascii": "[V]", "ext": "mp4", "dl": "download_video", "order": 7},
    "mind_map": {"icon": "🗺️", "ascii": "[M]", "ext": "json", "dl": "download_mind_map", "order": 8},
}


def icon_for(artifact_type: str) -> str:
    """Get display icon for artifact type (emoji or ASCII)."""
    info = ARTIFACT_TYPE_MAP.get(artifact_type, {"icon": "❔", "ascii": "[?]"})
    return info["ascii"] if USE_ASCII else info["icon"]


# ─────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────

def setup_logging(backup_dir: Path) -> Path | None:
    """Configure file logging. Returns log file path or None."""
    log_dir = backup_dir / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"manage-artifacts_{timestamp}.log"

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    fmt = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(message)s"
    file_handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))

    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.propagate = False  # Don't leak to console via root logger

    return log_path


# ─────────────────────────────────────────────────────────────────
# fzf helper
# ─────────────────────────────────────────────────────────────────

def fzf(
    items: list[str],
    *,
    multi: bool = False,
    header: str = "",
    prompt: str = "> ",
    with_nth: str | None = None,
    delimiter: str = "\t",
) -> list[str]:
    """Run fzf with given items and return selected lines.

    Returns empty list if user cancels (Esc) or no selection.
    """
    if not shutil.which("fzf"):
        print(f"{_e('❌', '[ERR]')} fzf がインストールされていません", file=sys.stderr)
        print("   インストール: apt install fzf / brew install fzf / choco install fzf", file=sys.stderr)
        sys.exit(1)

    cmd = ["fzf", "--delimiter", delimiter, "--reverse", "--border", "--height", "80%"]

    if multi:
        cmd.extend(["--multi", "--bind", "ctrl-a:select-all,ctrl-d:deselect-all"])
    if header:
        cmd.extend(["--header", header])
    if prompt:
        cmd.extend(["--prompt", prompt])
    if with_nth:
        cmd.extend(["--with-nth", with_nth])

    input_text = "\n".join(items)

    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=None,  # Let fzf render its UI to the terminal
            text=True,
        )
        if result.returncode != 0:
            return []  # User cancelled
        return [line for line in result.stdout.strip().split("\n") if line]
    except FileNotFoundError:
        print(f"{_e('❌', '[ERR]')} fzf がインストールされていません", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────
# Notebook selection
# ─────────────────────────────────────────────────────────────────

async def select_notebook(client: NotebookLMClient) -> tuple[str, str] | None:
    """Select a notebook via fzf. Returns (notebook_id, title) or None."""
    print(f"{_e('📓', '[NB]')} ノートブック一覧を取得中...")
    notebooks = await client.notebooks.list()

    if not notebooks:
        print(f"{_e('❌', '[ERR]')} ノートブックが見つかりません")
        return None

    # Sort by title
    notebooks.sort(key=lambda n: n.title)

    # Build TSV: id\ttitle\tcreated_at
    items = []
    max_title_len = max(len(n.title) for n in notebooks)
    for n in notebooks:
        created = n.created_at.strftime("%Y-%m-%d") if n.created_at else ""
        items.append(f"{n.id}\t{n.title:<{max_title_len}}  {created}")

    header = (
        f"ノートブック選択 ({len(notebooks)} 件)\n"
        f"{_sep(35)}\n"
        "操作: Enter=選択  Esc=キャンセル"
    )

    selected = fzf(items, header=header, prompt="ノートブック> ", with_nth="2..")

    if not selected:
        return None

    # Extract notebook_id from first column
    notebook_id = selected[0].split("\t")[0]
    title = selected[0].split("\t")[1].strip()
    # Remove trailing date
    title = re.sub(r"\s+\d{4}-\d{2}-\d{2}$", "", title).strip()

    return notebook_id, title


# ─────────────────────────────────────────────────────────────────
# Artifact listing and selection
# ─────────────────────────────────────────────────────────────────

@dataclass
class ArtifactInfo:
    """Artifact metadata for display and operations."""
    id: str
    title: str
    artifact_type: str
    created_at: str
    status: str


async def list_artifacts(client: NotebookLMClient, notebook_id: str) -> list[ArtifactInfo]:
    """List all artifacts in a notebook."""
    print(f"{_e('📋', '[..]')} アーティファクト一覧を取得中...")
    artifacts = await client.artifacts.list(notebook_id)

    result = []
    for a in artifacts:
        artifact_type = a.kind.value if hasattr(a, "kind") else "unknown"
        created_at = a.created_at.strftime("%Y-%m-%d %H:%M") if a.created_at else "----"
        result.append(ArtifactInfo(
            id=a.id,
            title=a.title or "(untitled)",
            artifact_type=artifact_type,
            created_at=created_at,
            status=a.status_str if hasattr(a, "status_str") else str(a.status),
        ))

    # Sort: type order → created_at
    def sort_key(a: ArtifactInfo):
        type_info = ARTIFACT_TYPE_MAP.get(a.artifact_type, {"order": 99})
        return (type_info["order"], a.created_at)

    result.sort(key=sort_key)
    return result


def select_artifacts(
    artifacts: list[ArtifactInfo], notebook_title: str
) -> list[ArtifactInfo]:
    """Select artifacts via fzf multi-select."""
    if not artifacts:
        print("   アーティファクトがありません")
        return []

    # Build TSV items
    items = []
    max_title_len = max(len(a.title) for a in artifacts)
    for a in artifacts:
        icon = icon_for(a.artifact_type)
        items.append(
            f"{a.id}\t{icon}  {a.title:<{max_title_len}}  {a.created_at}  {a.status}"
        )

    header = (
        f"アーティファクト管理 -- {notebook_title}\n"
        f"{_sep(48)}\n"
        f"全 {len(artifacts)} 件\n"
        "操作: Tab=選択/解除  Ctrl-A=全選択  Enter=確定  Esc=キャンセル"
    )

    selected = fzf(
        items, multi=True, header=header,
        prompt="アーティファクト> ", with_nth="2..",
    )

    if not selected:
        return []

    # Match selected IDs back to ArtifactInfo
    selected_ids = {line.split("\t")[0] for line in selected}
    return [a for a in artifacts if a.id in selected_ids]


def print_artifact_list(artifacts: list[ArtifactInfo], notebook_title: str) -> None:
    """Print artifact list to stdout (for --list-only mode)."""
    print(f"\n{_e('📓', '[NB]')} {notebook_title}")
    print(_sep())
    print(f"   全 {len(artifacts)} 件\n")

    if not artifacts:
        print("   (なし)")
        return

    max_title_len = max(len(a.title) for a in artifacts)
    current_type = None

    for a in artifacts:
        icon = icon_for(a.artifact_type)

        # Print type separator
        if a.artifact_type != current_type:
            current_type = a.artifact_type
            print(f"   -- {icon} {a.artifact_type} --")

        print(f"   {icon}  {a.title:<{max_title_len}}  {a.created_at}  {a.status}")

    print()


# ─────────────────────────────────────────────────────────────────
# Action selection
# ─────────────────────────────────────────────────────────────────

def select_action(count: int) -> str | None:
    """Select action via fzf. Returns action key or None."""
    items = [
        f"download_delete\t{_e('💾', '[SAVE]')}  ダウンロード + 削除（推奨）",
        f"download_only\t{_e('📥', '[DL]')}  ダウンロードのみ",
        f"delete_only\t{_e('🗑️', '[DEL]')}   削除のみ（バックアップなし）",
    ]

    header = (
        f"操作を選択してください ({count} 件選択中)\n"
        f"{_sep(31)}"
    )

    selected = fzf(items, header=header, prompt="操作> ", with_nth="2..")

    if not selected:
        return None

    return selected[0].split("\t")[0]


# ─────────────────────────────────────────────────────────────────
# Post-processing (standalone — no src/ imports)
# ─────────────────────────────────────────────────────────────────

def _remove_watermark(
    img: "Image.Image",
    wm_width: int = 130,
    wm_height: int = 25,
    margin_right: int = 5,
    margin_bottom: int = 5,
) -> "Image.Image":
    """Remove NotebookLM watermark by filling with sampled background color.

    Default dimensions are at 72 DPI. For higher DPI, pre-scale the values.
    """
    from PIL import ImageDraw

    img = img.convert("RGBA")
    img_w, img_h = img.size
    pixels = img.load()

    # Region coordinates (bottom-right corner)
    x = max(0, img_w - margin_right - wm_width)
    y = max(0, img_h - margin_bottom - wm_height)
    w = min(wm_width, img_w - x)
    h = min(wm_height, img_h - y)

    # Sample background color from edges adjacent to the region
    r_sum = g_sum = b_sum = a_sum = count = 0

    sx = min(x + w, img_w - 1)  # right edge
    for py in range(y, min(y + h, img_h)):
        p = pixels[sx, py]
        r_sum += p[0]; g_sum += p[1]; b_sum += p[2]; a_sum += p[3]
        count += 1

    sy = min(y + h, img_h - 1)  # bottom edge
    for px in range(x, min(x + w, img_w)):
        p = pixels[px, sy]
        r_sum += p[0]; g_sum += p[1]; b_sum += p[2]; a_sum += p[3]
        count += 1

    color = (
        (r_sum // count, g_sum // count, b_sum // count, a_sum // count)
        if count else (255, 255, 255, 255)
    )

    draw = ImageDraw.Draw(img)
    draw.rectangle([x, y, x + w - 1, y + h - 1], fill=color)
    return img


def _detect_and_remove_watermark(img: "Image.Image") -> "Image.Image":
    """Detect and remove NotebookLM watermark by scanning bottom-right corner.

    Unlike _remove_watermark (fixed dimensions), this auto-detects the watermark
    bounding box. Designed for infographics where WM size varies by aspect ratio.
    """
    from PIL import ImageDraw

    img = img.convert("RGBA")
    w, h = img.size
    pixels = img.load()

    # Scan region: bottom-right corner (250x35px)
    # WM is consistently within bottom 30px with ~10px bottom margin
    scan_w, scan_h = 250, 35
    x0 = max(0, w - scan_w)
    y0 = max(0, h - scan_h)

    # Initial background estimate from scan region corners (for WM detection)
    corners = [(x0, y0), (w - 1, y0), (x0, h - 1), (w - 1, h - 1)]
    r_s = g_s = b_s = 0
    for cx, cy in corners:
        p = pixels[cx, cy]
        r_s += p[0]; g_s += p[1]; b_s += p[2]
    bg_init = (r_s // 4, g_s // 4, b_s // 4)

    # Find dark pixels (watermark text) that differ from background
    threshold = 80
    min_x, min_y, max_x, max_y = w, h, 0, 0
    found = False

    for py in range(y0, h):
        for px in range(x0, w):
            p = pixels[px, py]
            diff = abs(p[0] - bg_init[0]) + abs(p[1] - bg_init[1]) + abs(p[2] - bg_init[2])
            if diff > threshold:
                min_x, min_y = min(min_x, px), min(min_y, py)
                max_x, max_y = max(max_x, px), max(max_y, py)
                found = True

    if not found:
        return img

    # Re-sample background from edges adjacent to detected WM (more accurate)
    r_s = g_s = b_s = a_s = count = 0
    sx = min(max_x + 1, w - 1)  # right edge
    for py in range(min_y, min(max_y + 1, h)):
        p = pixels[sx, py]
        r_s += p[0]; g_s += p[1]; b_s += p[2]; a_s += p[3]
        count += 1
    sy = min(max_y + 1, h - 1)  # bottom edge
    for px in range(min_x, min(max_x + 1, w)):
        p = pixels[px, sy]
        r_s += p[0]; g_s += p[1]; b_s += p[2]; a_s += p[3]
        count += 1
    bg = (
        (r_s // count, g_s // count, b_s // count, a_s // count)
        if count else (*bg_init, 255)
    )

    # Add buffer (3px each side) and fill with background
    buf = 3
    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [max(0, min_x - buf), max(0, min_y - buf),
         min(w - 1, max_x + buf), min(h - 1, max_y + buf)],
        fill=bg,
    )
    return img


def _pdf_to_images(
    pdf_path: Path, output_dir: Path, dpi: int = 150, fmt: str = "png"
) -> list[Path]:
    """Convert PDF to images using pdftoppm."""
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_dir / "slide"
    fmt_flag = "-png" if fmt == "png" else "-jpeg"
    ext = "png" if fmt == "png" else "jpg"
    subprocess.run(
        ["pdftoppm", fmt_flag, "-r", str(dpi), str(pdf_path), str(prefix)],
        check=True,
    )
    return sorted(output_dir.glob(f"slide-*.{ext}"))


def _images_to_pdf(
    images: list["Image.Image"], output_path: Path, dpi: int = 150
) -> None:
    """Create PDF from list of PIL images."""
    rgb = [img.convert("RGB") for img in images]
    rgb[0].save(
        output_path, "PDF", save_all=True, append_images=rgb[1:], resolution=dpi,
    )


def format_size(size: int) -> str:
    """Format file size in human-readable form."""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def compress_audio(input_path: Path, bitrate: str = "64k") -> tuple[Path, bool]:
    """Compress audio using ffmpeg (AAC)."""
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found — skipping audio compression")
        return input_path, False

    temp_path = input_path.with_suffix(".tmp.mp4")
    original_size = input_path.stat().st_size

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(input_path),
             "-c:a", "aac", "-b:a", bitrate, str(temp_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            if temp_path.exists():
                temp_path.unlink()
            return input_path, False

        compressed_size = temp_path.stat().st_size
        temp_path.replace(input_path)
        reduction = (1 - compressed_size / original_size) * 100
        logger.info(
            "compress audio: %s -> %s (%.0f%% reduction)",
            format_size(original_size), format_size(compressed_size), reduction,
        )
        return input_path, True

    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        return input_path, False


def _find_ghostscript() -> str | None:
    """Find ghostscript executable (gs on Unix, gswin64c/gswin32c on Windows)."""
    for name in ("gs", "gswin64c", "gswin32c"):
        if shutil.which(name):
            return name
    return None


def compress_slides(input_path: Path) -> tuple[Path, bool]:
    """Compress PDF using ghostscript."""
    gs_cmd = _find_ghostscript()
    if not gs_cmd:
        logger.warning("ghostscript not found — skipping PDF compression")
        return input_path, False

    temp_path = input_path.with_suffix(".tmp.pdf")
    original_size = input_path.stat().st_size

    try:
        result = subprocess.run(
            [gs_cmd, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
             "-dPDFSETTINGS=/printer", "-dNOPAUSE", "-dBATCH", "-dQUIET",
             f"-sOutputFile={temp_path}", str(input_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            if temp_path.exists():
                temp_path.unlink()
            return input_path, False

        compressed_size = temp_path.stat().st_size
        temp_path.replace(input_path)
        reduction = (1 - compressed_size / original_size) * 100
        logger.info(
            "compress slides: %s -> %s (%.0f%% reduction)",
            format_size(original_size), format_size(compressed_size), reduction,
        )
        return input_path, True

    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        return input_path, False


def _resize_half(path: Path) -> bool:
    """Resize image to 50% preserving aspect ratio. Returns True if resized."""
    from PIL import Image

    img = Image.open(path)
    w, h = img.size

    new_w = w // 2
    new_h = h // 2

    if new_w < 1 or new_h < 1:
        return False

    img = img.resize((new_w, new_h), Image.LANCZOS)
    img.save(path, "PNG", optimize=True)
    logger.info("infographic resized: %s (%dx%d → %dx%d)", path.name, w, h, new_w, new_h)
    return True


def postprocess(artifact_type: str, path: Path) -> str | None:
    """Apply post-processing based on artifact type. Returns summary or None."""
    if artifact_type == "audio":
        _, compressed = compress_audio(path)
        return "compressed" if compressed else None

    elif artifact_type == "slide_deck":
        return _postprocess_slides(path)

    elif artifact_type == "infographic":
        return _postprocess_infographic(path)

    return None


def _postprocess_infographic(path: Path) -> str | None:
    """Remove watermark and resize infographic (aspect-ratio preserving)."""
    from PIL import Image

    parts = []

    # Step 1: ウォーターマーク除去（原寸で実行 — パラメータが正確に一致）
    try:
        img = Image.open(path).convert("RGBA")
        img = _detect_and_remove_watermark(img)
        img.convert("RGB").save(path, "PNG", optimize=True)
        parts.append("watermark removed")
        logger.info("watermark removed: %s", path.name)
    except Exception as e:
        logger.warning("watermark removal failed: %s — %s", path.name, e)

    # Step 2: 1/2 リサイズ（全タイプ均等に50%縮小）
    resized = _resize_half(path)
    if resized:
        parts.append("resized")

    return ", ".join(parts) if parts else None


SLIDE_DPI = 150


def _postprocess_slides(path: Path) -> str | None:
    """Compress slides PDF and remove watermarks.

    Pipeline: compress → PDF→images → watermark removal → PDF reassembly
    """
    from PIL import Image

    parts = []

    # Step 1: Ghostscript compression
    _, compressed = compress_slides(path)
    if compressed:
        parts.append("compressed")

    # Step 2: Watermark removal (requires pdftoppm)
    if not shutil.which("pdftoppm"):
        logger.warning("pdftoppm not found — skipping slide watermark removal")
        return ", ".join(parts) if parts else None

    temp_dir = path.parent / f"_slide_wm_temp_{path.stem}"
    try:
        raw_images = _pdf_to_images(path, temp_dir, dpi=SLIDE_DPI, fmt="png")
        if not raw_images:
            logger.warning("no images extracted from PDF: %s", path.name)
            return ", ".join(parts) if parts else None

        # Scale watermark dimensions for slide DPI (base: 72 DPI)
        scale = SLIDE_DPI / 72.0
        clean_pil_images = []
        for raw_path in raw_images:
            img = Image.open(raw_path).convert("RGBA")
            img = _remove_watermark(
                img,
                wm_width=int(130 * scale),
                wm_height=int(25 * scale),
                margin_right=int(5 * scale),
                margin_bottom=int(5 * scale),
            )
            clean_pil_images.append(img)

        # Reassemble clean PDF (overwrite original)
        _images_to_pdf(clean_pil_images, path, dpi=SLIDE_DPI)
        parts.append("watermark removed")
        logger.info(
            "slide watermark removed: %s (%d pages)",
            path.name, len(clean_pil_images),
        )

    except Exception as e:
        logger.warning("slide watermark removal failed: %s — %s", path.name, e)
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

    return ", ".join(parts) if parts else None


# ─────────────────────────────────────────────────────────────────
# Download and delete operations
# ─────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    # Remove or replace invalid characters
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip("._")
    return name[:200] if name else "untitled"


def _resolve_output_path(artifact: ArtifactInfo, output_dir: Path) -> Path | None:
    """Resolve unique output path for an artifact (NOT thread-safe — call before parallel)."""
    type_info = ARTIFACT_TYPE_MAP.get(artifact.artifact_type)
    if not type_info:
        return None
    ext = type_info["ext"]
    date_prefix = artifact.created_at[:10].replace("-", "") if artifact.created_at else "nodate"
    filename = f"{date_prefix}_{artifact.artifact_type}_{sanitize_filename(artifact.title)}.{ext}"
    output_path = output_dir / filename
    stem = output_path.stem
    n = 2
    while output_path.exists():
        output_path = output_dir / f"{stem}_{n}.{ext}"
        n += 1
    return output_path


async def download_artifact(
    client: NotebookLMClient,
    notebook_id: str,
    artifact: ArtifactInfo,
    output_path: Path,
    dry_run: bool = False,
) -> Path | None:
    """Download a single artifact to the given output_path."""
    type_info = ARTIFACT_TYPE_MAP.get(artifact.artifact_type)
    if not type_info:
        logger.warning("unsupported type: %s (artifact=%s)", artifact.artifact_type, artifact.title)
        return None

    dl_method = type_info["dl"]

    if dry_run:
        logger.info("[DRY-RUN] download: %s -> %s", artifact.title, output_path.name)
        return output_path

    try:
        method = getattr(client.artifacts, dl_method)
        downloaded = await method(notebook_id, str(output_path), artifact_id=artifact.id)
        return Path(downloaded)

    except Exception as e:
        logger.error("download failed: %s — %s", artifact.title, e)
        return None


async def delete_artifact(
    client: NotebookLMClient,
    notebook_id: str,
    artifact: ArtifactInfo,
    dry_run: bool = False,
) -> bool:
    """Delete a single artifact."""
    if dry_run:
        logger.info("[DRY-RUN] delete: %s", artifact.title)
        return True

    try:
        await client.artifacts.delete(notebook_id, artifact.id)
        return True
    except Exception as e:
        logger.error("delete failed: %s — %s", artifact.title, e)
        return False


# ─────────────────────────────────────────────────────────────────
# Parallel execution engine
# ─────────────────────────────────────────────────────────────────

@dataclass
class TaskResult:
    """Result of processing a single artifact."""
    artifact: ArtifactInfo
    downloaded: bool
    download_path: Path | None
    download_size: int
    download_ms: float
    postprocessed: str | None
    postprocess_ms: float
    deleted: bool
    delete_ms: float
    error: str | None


async def process_one(
    client: NotebookLMClient,
    notebook_id: str,
    artifact: ArtifactInfo,
    output_path: Path | None,
    action: str,
    dry_run: bool,
    sem: asyncio.Semaphore,
    progress: dict,
    print_lock: asyncio.Lock,
) -> TaskResult:
    """Process a single artifact: download → postprocess → delete."""
    result = TaskResult(
        artifact=artifact,
        downloaded=False, download_path=None, download_size=0, download_ms=0,
        postprocessed=None, postprocess_ms=0,
        deleted=False, delete_ms=0,
        error=None,
    )
    needs_download = action in ("download_delete", "download_only")
    needs_delete = action in ("download_delete", "delete_only")
    icon = icon_for(artifact.artifact_type)

    async with sem:
        # Download
        if needs_download and output_path:
            t0 = time.monotonic()
            dl_path = await download_artifact(client, notebook_id, artifact, output_path, dry_run)
            result.download_ms = (time.monotonic() - t0) * 1000

            if dl_path is None and not dry_run:
                result.error = "download failed"
                logger.error("FAIL download: %s (%.0fms)", artifact.title, result.download_ms)
                async with print_lock:
                    progress["done"] += 1
                    n = progress["done"]
                    total = progress["total"]
                    print(f"  [{n}/{total}] {icon}  {artifact.title} -- {_e('⚠️', '[!]')} DL失敗")
                return result

            result.downloaded = True
            result.download_path = dl_path
            if dl_path and not dry_run:
                try:
                    result.download_size = dl_path.stat().st_size
                except OSError:
                    logger.warning("stat failed (file may not exist): %s", dl_path)
                    result.error = "download file not found after save"
                    return result

            logger.info(
                "OK download: %s (%s, %.0fms) -> %s",
                artifact.title, format_size(result.download_size),
                result.download_ms, dl_path.name if dl_path else "?",
            )

            # Post-processing (in thread to avoid blocking event loop)
            if dl_path and not dry_run:
                t1 = time.monotonic()
                pp_result = await asyncio.to_thread(postprocess, artifact.artifact_type, dl_path)
                result.postprocess_ms = (time.monotonic() - t1) * 1000
                result.postprocessed = pp_result
                if pp_result:
                    logger.info(
                        "OK postprocess: %s — %s (%.0fms)",
                        artifact.title, pp_result, result.postprocess_ms,
                    )

        # Delete
        if needs_delete:
            # Skip delete if download was required but failed
            if needs_download and not result.downloaded and not dry_run:
                result.error = result.error or "skipped delete (download failed)"
                return result

            t2 = time.monotonic()
            ok = await delete_artifact(client, notebook_id, artifact, dry_run)
            result.delete_ms = (time.monotonic() - t2) * 1000
            result.deleted = ok

            if ok:
                logger.info("OK delete: %s (%.0fms)", artifact.title, result.delete_ms)
            else:
                result.error = "delete failed"
                logger.error("FAIL delete: %s (%.0fms)", artifact.title, result.delete_ms)

        # Progress output
        async with print_lock:
            progress["done"] += 1
            n = progress["done"]
            total = progress["total"]
            total_ms = result.download_ms + result.postprocess_ms + result.delete_ms

            parts = []
            if result.downloaded:
                parts.append(f"{_e('📥', '[DL]')} {format_size(result.download_size)}")
            if result.postprocessed:
                parts.append(f"{_e('📦', '[PP]')} {result.postprocessed}")
            if result.deleted:
                parts.append(f"{_e('🗑️', '[DEL]')} 削除")
            if result.error:
                parts.append(f"{_e('⚠️', '[!]')} {result.error}")
            if dry_run:
                parts.append("[DRY-RUN]")

            detail = " / ".join(parts) if parts else "OK"
            print(f"  [{n}/{total}] {icon}  {artifact.title} -- {detail} ({total_ms:.0f}ms)")

    return result


async def execute_parallel(
    client: NotebookLMClient,
    notebook_id: str,
    selected: list[ArtifactInfo],
    output_dir: Path | None,
    action: str,
    dry_run: bool,
    concurrency: int,
) -> list[TaskResult]:
    """Execute download/delete operations in parallel with semaphore control."""
    sem = asyncio.Semaphore(concurrency)
    print_lock = asyncio.Lock()
    progress = {"done": 0, "total": len(selected)}

    # Pre-compute unique output paths (single-threaded to avoid race conditions)
    needs_download = action in ("download_delete", "download_only")
    output_paths: list[Path | None] = []
    if needs_download and output_dir:
        seen: set[Path] = set()
        for artifact in selected:
            path = _resolve_output_path(artifact, output_dir)
            # Also avoid collisions within this batch
            if path:
                stem, ext = path.stem, path.suffix
                n = 2
                while path in seen:
                    path = output_dir / f"{stem}_{n}{ext}"
                    n += 1
                seen.add(path)
            output_paths.append(path)
    else:
        output_paths = [None] * len(selected)

    tasks = [
        process_one(
            client, notebook_id, artifact, out_path, action, dry_run,
            sem, progress, print_lock,
        )
        for artifact, out_path in zip(selected, output_paths)
    ]

    return await asyncio.gather(*tasks)


# ─────────────────────────────────────────────────────────────────
# Main flow
# ─────────────────────────────────────────────────────────────────

async def main() -> int:
    parser = argparse.ArgumentParser(
        description="NotebookLM アーティファクト管理ツール",
    )
    parser.add_argument("-n", "--notebook-id", help="ノートブックID直接指定")
    parser.add_argument("-l", "--list-only", action="store_true", help="一覧表示のみ")
    parser.add_argument("-d", "--dry-run", action="store_true", help="実行せずにプレビュー")
    parser.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"同時実行数 (デフォルト: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--ascii", action="store_true", help="ASCII文字のみ使用（絵文字なし）")
    parser.add_argument("--emoji", action="store_true", help="絵文字を強制使用")
    args = parser.parse_args()

    # Apply explicit --ascii / --emoji flags
    global USE_ASCII
    if args.ascii:
        USE_ASCII = True
    elif args.emoji:
        USE_ASCII = False

    timeout = int(os.environ.get("NOTEBOOKLM_TIMEOUT", DEFAULT_TIMEOUT))
    backup_dir = Path(os.environ.get("ARTIFACT_BACKUP_DIR", DEFAULT_BACKUP_DIR))

    # Setup logging
    log_path = setup_logging(backup_dir)

    print(_sep())
    print("  NotebookLM アーティファクト管理ツール")
    print(_sep())
    print()

    logger.info("=== session start ===")
    logger.info("timeout=%ds, backup_dir=%s, dry_run=%s, concurrency=%d, ascii=%s",
                timeout, backup_dir, args.dry_run, args.concurrency, USE_ASCII)
    if log_path:
        print(f"{_e('📝', '[LOG]')} ログ: {log_path}")
        print()

    async with await NotebookLMClient.from_storage(timeout=timeout) as client:

        # Step 1: Select notebook
        if args.notebook_id:
            notebook_id = args.notebook_id
            # Fetch title
            try:
                nb = await client.notebooks.get(notebook_id)
                notebook_title = nb.title if nb else notebook_id[:8]
            except Exception:
                notebook_title = notebook_id[:8]
        else:
            result = await select_notebook(client)
            if not result:
                print("キャンセルされました")
                return 0
            notebook_id, notebook_title = result

        print(f"\n{_e('📓', '[NB]')} {notebook_title}")
        print()
        logger.info("notebook: %s (%s)", notebook_title, notebook_id)

        # Step 2: List artifacts
        artifacts = await list_artifacts(client, notebook_id)
        logger.info("artifacts found: %d", len(artifacts))

        if args.list_only:
            print_artifact_list(artifacts, notebook_title)
            return 0

        if not artifacts:
            print("   アーティファクトがありません")
            return 0

        # Step 3: Select artifacts
        selected = select_artifacts(artifacts, notebook_title)
        if not selected:
            print("キャンセルされました")
            return 0

        print(f"\n   {len(selected)} 件選択\n")
        logger.info("selected: %d artifacts", len(selected))

        # Step 4: Select action
        action = select_action(len(selected))
        if not action:
            print("キャンセルされました")
            return 0

        logger.info("action: %s", action)

        # Extra warning for delete-only
        if action == "delete_only":
            print()
            print(f"{_e('⚠️', '[!]')}  バックアップなしで削除します。この操作は取り消せません。")
            confirm = input("本当に削除しますか？ [y/N] ").strip().lower()
            if confirm != "y":
                print("キャンセルされました")
                return 0

        # Prepare download directory
        needs_download = action in ("download_delete", "download_only")
        output_dir = None
        if needs_download:
            dir_name = sanitize_filename(notebook_title)
            output_dir = backup_dir / dir_name
            if not args.dry_run:
                output_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n{_e('📁', '[DIR]')} 保存先: {output_dir}")

        # Step 5: Execute (parallel)
        print()
        start_time = time.monotonic()

        results = await execute_parallel(
            client, notebook_id, selected, output_dir, action,
            args.dry_run, args.concurrency,
        )

        elapsed = time.monotonic() - start_time

        # Summary
        success = sum(1 for r in results if not r.error)
        fail = sum(1 for r in results if r.error)
        total_dl_size = sum(r.download_size for r in results)
        total_dl_ms = sum(r.download_ms for r in results)
        total_pp_ms = sum(r.postprocess_ms for r in results)
        total_del_ms = sum(r.delete_ms for r in results)

        print()
        print(_sep())
        action_label = {"download_delete": "DL+削除", "download_only": "DL", "delete_only": "削除"}
        print(f"  完了: {success}/{len(selected)} 成功", end="")
        if fail:
            print(f", {fail} 失敗", end="")
        print(f"  ({action_label.get(action, action)}, {elapsed:.1f}秒, 並列={args.concurrency})")
        if total_dl_size:
            print(f"  DL合計: {format_size(total_dl_size)}")
        if output_dir:
            print(f"  保存先: {output_dir}")
        if log_path:
            print(f"  ログ: {log_path}")
        print(_sep())

        logger.info(
            "=== session end === success=%d, fail=%d, elapsed=%.1fs, "
            "dl_total=%s (%.0fms), postprocess=%.0fms, delete=%.0fms",
            success, fail, elapsed,
            format_size(total_dl_size), total_dl_ms, total_pp_ms, total_del_ms,
        )

        # Log individual failures
        for r in results:
            if r.error:
                logger.warning("FAILED: %s — %s", r.artifact.title, r.error)

    return 0


if __name__ == "__main__":
    # Windows: ensure UTF-8 output for emoji/Japanese text
    if sys.platform == "win32":
        os.environ.setdefault("PYTHONUTF8", "1")
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main()))
