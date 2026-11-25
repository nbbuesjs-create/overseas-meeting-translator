"""Entry point for Meeting Subtitle Translator."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def _check_dependencies() -> list[str]:
    missing: list[str] = []
    for mod, pkg in (
        ("PyQt6", "PyQt6"),
        ("pyaudiowpatch", "PyAudioWPatch"),
        ("numpy", "numpy"),
        ("requests", "requests"),
    ):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    return missing


def _log_crash(exc_text: str) -> Path:
    log_path = APP_DIR / "error.log"
    log_path.write_text(exc_text, encoding="utf-8")
    return log_path


def main() -> int:
    missing = _check_dependencies()
    if missing:
        print("缺少以下 Python 依赖包，请先运行 start.bat 或执行 pip install -r requirements.txt：")
        print("  " + ", ".join(missing))
        return 1

    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName("海外会议翻译助手")
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Microsoft YaHei UI", 10))

    try:
        from config import AppConfig
        from ui_overlay import SubtitleOverlayWindow

        cfg = AppConfig.load()
        window = SubtitleOverlayWindow(cfg)
        window.show()
        return app.exec()
    except Exception:
        tb = traceback.format_exc()
        log_path = _log_crash(tb)
        QMessageBox.critical(
            None,
            "程序启动异常",
            f"运行过程中发生错误，详细日志已保存至：\n{log_path}\n\n{tb[-600:]}",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
