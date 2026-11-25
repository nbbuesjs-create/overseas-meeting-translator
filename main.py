"""Entry point for Overseas Meeting Translator (Windows & macOS)."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def _check_dependencies() -> list[str]:
    missing: list[str] = []
    deps: list[tuple[str, str]] = [
        ("PyQt6", "PyQt6"),
        ("numpy", "numpy"),
        ("requests", "requests"),
    ]
    if sys.platform == "win32":
        deps.append(("pyaudiowpatch", "PyAudioWPatch"))
    else:
        deps.append(("sounddevice", "sounddevice"))

    for mod, pkg in deps:
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
        launcher = "start.bat" if sys.platform == "win32" else "./start_mac.command"
        print(f"缺少以下 Python 依赖包，请先运行 {launcher} 或执行 pip install -r requirements.txt：")
        print("  " + ", ".join(missing))
        return 1

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "OverseasMeetingTranslator.Desktop.1"
            )
        except Exception:
            pass

    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName("海外会议翻译助手")
    app.setQuitOnLastWindowClosed(False)
    font_family = "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei UI"
    app.setFont(QFont(font_family, 10))

    try:
        from config import AppConfig
        from ui_overlay import SubtitleOverlayWindow, create_app_icon

        app.setWindowIcon(create_app_icon())
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
