#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pillow",
# ]
# ///
"""Batch post-process downloaded artifacts (slides + infographics).

Retroactively applies post-processing to files that were downloaded
before external tools (ghostscript, pdftoppm) were installed.

- Slides (PDF):  ghostscript compression + watermark removal
- Infographics (PNG): watermark auto-detection + 1/2 resize

Usage:
    uv run patch-postprocess.py /path/to/artifacts
    uv run patch-postprocess.py /path/to/artifacts --dry-run
    uv run patch-postprocess.py /path/to/artifacts --slides-only
    uv run patch-postprocess.py /path/to/artifacts --infographics-only
"""

import argparse
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ─────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────

def _find_ghostscript() -> str | None:
    for name in ("gs", "gswin64c", "gswin32c"):
        if shutil.which(name):
            return name
    return None


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


# ─────────────────────────────────────────────────────────────────
# Slide post-processing (PDF)
# ─────────────────────────────────────────────────────────────────

SLIDE_DPI = 150


def compress_pdf(gs_cmd: str, path: Path) -> tuple[bool, int, int]:
    """Compress PDF with ghostscript. Returns (success, original_size, new_size)."""
    original_size = path.stat().st_size
    temp_path = path.with_suffix(".tmp.pdf")

    try:
        result = subprocess.run(
            [gs_cmd, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
             "-dPDFSETTINGS=/printer", "-dNOPAUSE", "-dBATCH", "-dQUIET",
             f"-sOutputFile={temp_path}", str(path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            if temp_path.exists():
                temp_path.unlink()
            return False, original_size, original_size

        new_size = temp_path.stat().st_size
        temp_path.replace(path)
        return True, original_size, new_size

    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        return False, original_size, original_size


def _remove_watermark_fixed(img, wm_width=130, wm_height=25, margin_right=5, margin_bottom=5):
    """Remove watermark with fixed dimensions (for slides at known DPI)."""
    from PIL import ImageDraw

    img = img.convert("RGBA")
    img_w, img_h = img.size
    pixels = img.load()

    x = max(0, img_w - margin_right - wm_width)
    y = max(0, img_h - margin_bottom - wm_height)
    w = min(wm_width, img_w - x)
    h = min(wm_height, img_h - y)

    r_sum = g_sum = b_sum = a_sum = count = 0
    sx = min(x + w, img_w - 1)
    for py in range(y, min(y + h, img_h)):
        p = pixels[sx, py]
        r_sum += p[0]; g_sum += p[1]; b_sum += p[2]; a_sum += p[3]
        count += 1

    sy = min(y + h, img_h - 1)
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


def remove_slide_watermarks(path: Path) -> tuple[bool, int]:
    """Remove watermarks from all slide pages. Returns (success, page_count)."""
    from PIL import Image

    if not shutil.which("pdftoppm"):
        return False, 0

    temp_dir = path.parent / f"_patch_wm_{path.stem}"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        prefix = temp_dir / "slide"
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(SLIDE_DPI), str(path), str(prefix)],
            check=True,
        )
        raw_images = sorted(temp_dir.glob("slide-*.png"))
        if not raw_images:
            return False, 0

        scale = SLIDE_DPI / 72.0
        clean_images = []
        for raw_path in raw_images:
            img = Image.open(raw_path).convert("RGBA")
            img = _remove_watermark_fixed(
                img,
                wm_width=int(130 * scale),
                wm_height=int(25 * scale),
                margin_right=int(5 * scale),
                margin_bottom=int(5 * scale),
            )
            clean_images.append(img)

        rgb = [img.convert("RGB") for img in clean_images]
        rgb[0].save(
            path, "PDF", save_all=True, append_images=rgb[1:], resolution=SLIDE_DPI,
        )
        return True, len(clean_images)

    except Exception as e:
        print(f"    ERROR: {e}")
        return False, 0
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


def process_slide(pdf: Path, gs_cmd: str | None, has_pdftoppm: bool) -> tuple[str, int]:
    """Process one slide PDF. Returns (summary, bytes_saved)."""
    original_size = pdf.stat().st_size
    parts = []

    if gs_cmd:
        ok, orig, new = compress_pdf(gs_cmd, pdf)
        if ok:
            pct = (1 - new / orig) * 100 if orig > 0 else 0
            parts.append(f"compressed {format_size(orig)}->{format_size(new)} ({pct:.0f}%)")
        else:
            parts.append("compress FAILED")

    if has_pdftoppm:
        ok, pages = remove_slide_watermarks(pdf)
        if ok:
            parts.append(f"WM removed ({pages}p)")
        else:
            parts.append("WM FAILED")

    final_size = pdf.stat().st_size
    return " / ".join(parts) if parts else "skipped", original_size - final_size


# ─────────────────────────────────────────────────────────────────
# Infographic post-processing (PNG)
# ─────────────────────────────────────────────────────────────────

def _detect_and_remove_watermark(img):
    """Detect and remove watermark by scanning bottom-right corner."""
    from PIL import ImageDraw

    img = img.convert("RGBA")
    w, h = img.size
    pixels = img.load()

    scan_w, scan_h = 250, 35
    x0 = max(0, w - scan_w)
    y0 = max(0, h - scan_h)

    corners = [(x0, y0), (w - 1, y0), (x0, h - 1), (w - 1, h - 1)]
    r_s = g_s = b_s = 0
    for cx, cy in corners:
        p = pixels[cx, cy]
        r_s += p[0]; g_s += p[1]; b_s += p[2]
    bg_init = (r_s // 4, g_s // 4, b_s // 4)

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

    r_s = g_s = b_s = a_s = count = 0
    sx = min(max_x + 1, w - 1)
    for py in range(min_y, min(max_y + 1, h)):
        p = pixels[sx, py]
        r_s += p[0]; g_s += p[1]; b_s += p[2]; a_s += p[3]
        count += 1
    sy = min(max_y + 1, h - 1)
    for px in range(min_x, min(max_x + 1, w)):
        p = pixels[px, sy]
        r_s += p[0]; g_s += p[1]; b_s += p[2]; a_s += p[3]
        count += 1
    bg = (
        (r_s // count, g_s // count, b_s // count, a_s // count)
        if count else (*bg_init, 255)
    )

    buf = 3
    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [max(0, min_x - buf), max(0, min_y - buf),
         min(w - 1, max_x + buf), min(h - 1, max_y + buf)],
        fill=bg,
    )
    return img


def process_infographic(png: Path) -> tuple[str, int]:
    """Process one infographic PNG. Returns (summary, bytes_saved)."""
    from PIL import Image

    original_size = png.stat().st_size
    parts = []

    try:
        img = Image.open(png).convert("RGBA")
        orig_w, orig_h = img.size

        # Step 1: Watermark removal (at original size)
        img = _detect_and_remove_watermark(img)
        img.convert("RGB").save(png, "PNG", optimize=True)
        parts.append("WM removed")

        # Step 2: 1/2 resize
        new_w, new_h = orig_w // 2, orig_h // 2
        if new_w > 0 and new_h > 0:
            img = Image.open(png)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            img.save(png, "PNG", optimize=True)
            parts.append(f"resized {orig_w}x{orig_h}->{new_w}x{new_h}")

    except Exception as e:
        parts.append(f"ERROR: {e}")

    final_size = png.stat().st_size
    return " / ".join(parts) if parts else "skipped", original_size - final_size


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Batch post-process downloaded artifacts (slides + infographics)",
    )
    parser.add_argument("directory", type=Path, help="Directory containing artifact files")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("-c", "--concurrency", type=int, default=2, help="Parallel workers (default: 2)")
    parser.add_argument("--slides-only", action="store_true", help="Process slides (PDF) only")
    parser.add_argument("--infographics-only", action="store_true", help="Process infographics (PNG) only")
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"ERROR: {args.directory} is not a directory")
        return 1

    do_slides = not args.infographics_only
    do_infographics = not args.slides_only

    pdfs = sorted(args.directory.glob("*slide_deck*.pdf")) if do_slides else []
    pngs = sorted(args.directory.glob("*infographic*.png")) if do_infographics else []

    if not pdfs and not pngs:
        print(f"No matching files found in {args.directory}")
        print("  (looking for *slide_deck*.pdf and *infographic*.png)")
        return 0

    gs_cmd = _find_ghostscript()
    has_pdftoppm = bool(shutil.which("pdftoppm"))

    print(f"Directory: {args.directory}")
    if pdfs:
        print(f"  Slides (PDF): {len(pdfs)} files")
        print(f"    ghostscript: {'OK (' + gs_cmd + ')' if gs_cmd else 'NOT FOUND — compression skipped'}")
        print(f"    pdftoppm:    {'OK' if has_pdftoppm else 'NOT FOUND — watermark removal skipped'}")
    if pngs:
        print(f"  Infographics (PNG): {len(pngs)} files")
        print(f"    Pillow: OK")
    print()

    if args.dry_run:
        print("[DRY-RUN] Would process:")
        for f in pdfs + pngs:
            print(f"  {f.name} ({format_size(f.stat().st_size)})")
        return 0

    total = len(pdfs) + len(pngs)
    total_saved = 0
    done = 0

    def _process(path: Path) -> tuple[Path, str, int]:
        if path.suffix == ".pdf":
            return (path, *process_slide(path, gs_cmd, has_pdftoppm))
        else:
            return (path, *process_infographic(path))

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(_process, f): f for f in pdfs + pngs}
        for future in as_completed(futures):
            done += 1
            path, detail, saved = future.result()
            total_saved += saved
            print(f"  [{done}/{total}] {path.name} -- {detail}")

    print()
    print(f"Done: {total} files processed, {format_size(total_saved)} saved")


if __name__ == "__main__":
    if sys.platform == "win32":
        import os
        os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main() or 0)
