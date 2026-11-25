"""Generate transparent multi-size app icon (.png / .ico) and install Windows Start Menu shortcut."""

from __future__ import annotations

import base64
import ctypes
import os
import struct
import subprocess
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QGuiApplication, QImage

APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
ICON_PNG_PATH = ASSETS_DIR / "app_icon.png"
ICON_ICO_PATH = ASSETS_DIR / "app_icon.ico"
APP_DISPLAY_NAME = "海外会议翻译助手"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _find_source_image(cli_arg: str | None) -> Path | None:
    if cli_arg:
        p = Path(cli_arg.strip().strip('"'))
        if p.exists():
            return p

    hint_file = APP_DIR / ".icon_source_path.txt"
    if hint_file.exists():
        raw = hint_file.read_text(encoding="utf-8").strip().splitlines()
        if raw:
            p = Path(raw[0].strip().strip('"'))
            if p.exists():
                return p

    for candidate in (
        ASSETS_DIR / "app_icon_src.png",
        ASSETS_DIR / "app_icon_src.jpg",
        ICON_PNG_PATH,
    ):
        if candidate.exists():
            return candidate
    return None


def _remove_black_background_floodfill(src_img: QImage) -> QImage:
    """Remove solid black outer background via border flood-fill with smooth anti-aliased edges."""
    if max(src_img.width(), src_img.height()) > 512:
        src_img = src_img.scaled(
            512,
            512,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    img = src_img.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    bpl = img.bytesPerLine()
    rgba = (
        np.frombuffer(ptr, dtype=np.uint8)
        .reshape((h, bpl))[:, : w * 4]
        .reshape((h, w, 4))
        .copy()
    )

    corners_alpha = [
        int(rgba[0, 0, 3]),
        int(rgba[0, w - 1, 3]),
        int(rgba[h - 1, 0, 3]),
        int(rgba[h - 1, w - 1, 3]),
    ]
    already_transparent = all(a < 16 for a in corners_alpha)

    if not already_transparent:
        max_rgb = rgba[:, :, :3].max(axis=2)
        bg_thresh = 36
        visited = np.zeros((h, w), dtype=bool)
        is_bg = np.zeros((h, w), dtype=bool)
        q: deque[tuple[int, int]] = deque()

        for x in range(w):
            if max_rgb[0, x] <= bg_thresh:
                visited[0, x] = True
                is_bg[0, x] = True
                q.append((0, x))
            if max_rgb[h - 1, x] <= bg_thresh:
                visited[h - 1, x] = True
                is_bg[h - 1, x] = True
                q.append((h - 1, x))
        for y in range(1, h - 1):
            if max_rgb[y, 0] <= bg_thresh:
                visited[y, 0] = True
                is_bg[y, 0] = True
                q.append((y, 0))
            if max_rgb[y, w - 1] <= bg_thresh:
                visited[y, w - 1] = True
                is_bg[y, w - 1] = True
                q.append((y, w - 1))

        while q:
            cy, cx = q.popleft()
            if cy > 0 and not visited[cy - 1, cx] and max_rgb[cy - 1, cx] <= bg_thresh:
                visited[cy - 1, cx] = True
                is_bg[cy - 1, cx] = True
                q.append((cy - 1, cx))
            if cy + 1 < h and not visited[cy + 1, cx] and max_rgb[cy + 1, cx] <= bg_thresh:
                visited[cy + 1, cx] = True
                is_bg[cy + 1, cx] = True
                q.append((cy + 1, cx))
            if cx > 0 and not visited[cy, cx - 1] and max_rgb[cy, cx - 1] <= bg_thresh:
                visited[cy, cx - 1] = True
                is_bg[cy, cx - 1] = True
                q.append((cy, cx - 1))
            if cx + 1 < w and not visited[cy, cx + 1] and max_rgb[cy, cx + 1] <= bg_thresh:
                visited[cy, cx + 1] = True
                is_bg[cy, cx + 1] = True
                q.append((cy, cx + 1))

        rgba[is_bg, 3] = 0

        bg_dil1 = is_bg.copy()
        bg_dil1[1:, :] |= is_bg[:-1, :]
        bg_dil1[:-1, :] |= is_bg[1:, :]
        bg_dil1[:, 1:] |= is_bg[:, :-1]
        bg_dil1[:, :-1] |= is_bg[:, 1:]

        bg_dil2 = bg_dil1.copy()
        bg_dil2[1:, :] |= bg_dil1[:-1, :]
        bg_dil2[:-1, :] |= bg_dil1[1:, :]
        bg_dil2[:, 1:] |= bg_dil1[:, :-1]
        bg_dil2[:, :-1] |= bg_dil1[:, 1:]

        fringe = bg_dil2 & (~is_bg)
        fringe_ys, fringe_xs = np.where(fringe)
        for fy, fx in zip(fringe_ys, fringe_xs):
            m = int(max_rgb[fy, fx])
            if m < 120:
                alpha_f = max(0.0, min(1.0, (m - 22.0) / 88.0))
                rgba[fy, fx, 3] = int(round(alpha_f * 255.0))
                if alpha_f > 0.08:
                    boost = min(1.8, 1.0 / (alpha_f ** 0.55))
                    rgba[fy, fx, 0] = min(255, int(rgba[fy, fx, 0] * boost))
                    rgba[fy, fx, 1] = min(255, int(rgba[fy, fx, 1] * boost))
                    rgba[fy, fx, 2] = min(255, int(rgba[fy, fx, 2] * boost))

    ys, xs = np.where(rgba[:, :, 3] > 20)
    if len(ys) > 0 and len(xs) > 0:
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        cropped = rgba[y0:y1, x0:x1]
        ch, cw = cropped.shape[:2]
        side = int(round(max(ch, cw) * 1.08))
        canvas = np.zeros((side, side, 4), dtype=np.uint8)
        oy = (side - ch) // 2
        ox = (side - cw) // 2
        canvas[oy : oy + ch, ox : ox + cw] = cropped
        rgba = canvas
        h, w = side, side

    out_bytes = np.ascontiguousarray(rgba, dtype=np.uint8).tobytes()
    out_img = QImage(out_bytes, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
    return out_img.scaled(
        512,
        512,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _qimage_to_png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    return data


def _write_multi_size_ico(img: QImage, ico_path: Path) -> None:
    """Write a standard PNG-compressed multi-resolution Windows .ico file."""
    frames: list[tuple[int, bytes]] = []
    for sz in ICO_SIZES:
        scaled = img.scaled(
            sz,
            sz,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        frames.append((sz, _qimage_to_png_bytes(scaled)))

    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    dir_entries: list[bytes] = []
    payloads: list[bytes] = []

    for sz, png_data in frames:
        wb = 0 if sz >= 256 else sz
        hb = 0 if sz >= 256 else sz
        dir_entries.append(
            struct.pack("<BBBBHHII", wb, hb, 0, 0, 1, 32, len(png_data), offset)
        )
        payloads.append(png_data)
        offset += len(png_data)

    ico_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.write_bytes(header + b"".join(dir_entries) + b"".join(payloads))


def build_icons(src_arg: str | None = None) -> bool:
    src = _find_source_image(src_arg)
    if src is None:
        if ICON_ICO_PATH.exists():
            print(f"[INFO] Using existing icon: {ICON_ICO_PATH}")
            return True
        print("[ERROR] No source icon image found.")
        return False

    print(f"[1/2] Processing transparent icon from: {src}")
    raw_img = QImage(str(src))
    if raw_img.isNull():
        print(f"[ERROR] Failed to load image: {src}")
        return False

    clean_img = _remove_black_background_floodfill(raw_img)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    clean_img.save(str(ICON_PNG_PATH), "PNG")
    _write_multi_size_ico(clean_img, ICON_ICO_PATH)
    print(f"      Saved PNG: {ICON_PNG_PATH}")
    print(f"      Saved ICO: {ICON_ICO_PATH}")
    return True


def _create_windows_shortcut(lnk_path: Path, target: Path, args: str, icon_path: Path) -> None:
    def _ps_escape(s: str) -> str:
        return s.replace("'", "''")

    ps_script = (
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$sc = $ws.CreateShortcut('{_ps_escape(str(lnk_path))}')\n"
        f"$sc.TargetPath = '{_ps_escape(str(target))}'\n"
        f"$sc.Arguments = '{_ps_escape(args)}'\n"
        f"$sc.WorkingDirectory = '{_ps_escape(str(APP_DIR))}'\n"
        f"$sc.IconLocation = '{_ps_escape(str(icon_path))},0'\n"
        f"$sc.Description = '{_ps_escape(APP_DISPLAY_NAME)}'\n"
        "$sc.Save()\n"
    )
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    ps_exe = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    subprocess.run(
        [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        check=True,
    )


def install_start_menu_shortcut() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("Cannot locate APPDATA directory.")

    programs_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    programs_dir.mkdir(parents=True, exist_ok=True)
    lnk_path = programs_dir / f"{APP_DISPLAY_NAME}.lnk"

    venv_pythonw = APP_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if venv_pythonw.exists():
        target = venv_pythonw
        args = "main.py"
    else:
        target = APP_DIR / "start.bat"
        args = ""

    icon_file = ICON_ICO_PATH if ICON_ICO_PATH.exists() else target
    print(f"[2/2] Installing Start Menu shortcut: {lnk_path}")
    _create_windows_shortcut(lnk_path, target, args, icon_file)

    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass

    return lnk_path


def main() -> int:
    _app = QGuiApplication(sys.argv)
    src_arg = sys.argv[1] if len(sys.argv) > 1 else None

    if not build_icons(src_arg):
        return 1

    try:
        lnk_path = install_start_menu_shortcut()
    except Exception as exc:
        print(f"[ERROR] Failed to create Start Menu shortcut: {exc}")
        return 1

    print("")
    print("==========================================================")
    print("  [SUCCESS] Icon generated & added to Windows Start Menu!")
    print(f"  Shortcut: {lnk_path}")
    print("  Tip: Press the Win key, search for the app name, and")
    print("       right-click -> 'Pin to Start' or 'Pin to taskbar'.")
    print("==========================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
