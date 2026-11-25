"""Always-on-top translucent floating subtitle overlay window."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QPoint, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from audio_capture import AudioCaptureManager, AudioSegment
from config import AUDIO_MODES, TRANSCRIPTS_DIR, TRANSLATION_DIRECTIONS, AppConfig
from translator import TranslationEngine, TranslationResult
from ui_settings import SettingsDialog


def create_app_icon() -> QIcon:
    """Render a crisp vector-like tray/window icon in memory (no external .ico required)."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Rounded indigo badge
    path = QPainterPath()
    path.addRoundedRect(QRectF(4, 4, 56, 56), 14, 14)
    p.fillPath(path, QColor("#2563eb"))

    # Stylized subtitle lines + translate symbol
    p.setPen(QPen(QColor("#ffffff"), 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(16, 24, 48, 24)
    p.setPen(QPen(QColor("#93c5fd"), 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(16, 36, 42, 36)
    p.drawLine(16, 46, 32, 46)
    p.end()
    return QIcon(pix)


@dataclass
class TranscriptEntry:
    seq_id: int
    timestamp: str
    channel_id: str
    channel_label: str
    source_lang: str
    original: str
    translation: str


class LevelMeterBar(QWidget):
    """Thin live audio volume indicator bar (supports single or dual channel)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(5)
        self._sys_level = 0.0
        self._mic_level = 0.0
        self._mode = "system"

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._sys_level = 0.0
        self._mic_level = 0.0
        self.update()

    def set_level(self, channel_id: str, rms: float) -> None:
        # Scale typical speech RMS (0.0 - 0.15) into 0.0 - 1.0 with slight smoothing
        norm = min(1.0, rms * 8.5)
        if channel_id == "system":
            self._sys_level = max(norm, self._sys_level * 0.65)
        else:
            self._mic_level = max(norm, self._mic_level * 0.65)
        self.update()

    def reset(self) -> None:
        self._sys_level = 0.0
        self._mic_level = 0.0
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()

        p.fillRect(0, 0, w, h, QColor(255, 255, 255, 18))

        if self._mode == "both":
            half = max(1, (w - 6) // 2)
            sys_w = int(half * self._sys_level)
            mic_w = int(half * self._mic_level)
            if sys_w > 0:
                p.fillRect(0, 0, sys_w, h, QColor("#38bdf8"))
            if mic_w > 0:
                p.fillRect(half + 6, 0, mic_w, h, QColor("#34d399"))
        elif self._mode == "mic":
            mic_w = int(w * self._mic_level)
            if mic_w > 0:
                p.fillRect(0, 0, mic_w, h, QColor("#34d399"))
        else:
            sys_w = int(w * self._sys_level)
            if sys_w > 0:
                p.fillRect(0, 0, sys_w, h, QColor("#38bdf8"))
        p.end()


class SubtitleCard(QFrame):
    """Single bilingual subtitle card inside the floating overlay."""

    def __init__(
        self,
        timestamp: str,
        channel_id: str,
        channel_label: str,
        font_size: int,
        show_original: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SubtitleCard")
        self._show_original = show_original

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(3)

        # Header meta row
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)

        badge_color = "#0284c7" if channel_id == "system" else "#059669"
        icon = "🔊" if channel_id == "system" else "🎤"
        self.lbl_channel = QLabel(f"{icon} {channel_label}")
        self.lbl_channel.setStyleSheet(
            f"background-color: {badge_color}; color: #ffffff; border-radius: 4px; "
            "padding: 1px 6px; font-size: 11px; font-weight: bold;"
        )
        meta_row.addWidget(self.lbl_channel)

        self.lbl_lang = QLabel("...")
        self.lbl_lang.setStyleSheet(
            "background-color: rgba(148, 163, 184, 0.22); color: #cbd5e1; "
            "border-radius: 4px; padding: 1px 5px; font-size: 10px;"
        )
        self.lbl_lang.setVisible(False)
        meta_row.addWidget(self.lbl_lang)

        self.lbl_time = QLabel(timestamp)
        self.lbl_time.setStyleSheet("color: #94a3b8; font-size: 11px;")
        meta_row.addWidget(self.lbl_time)

        self.lbl_latency = QLabel("")
        self.lbl_latency.setStyleSheet("color: #64748b; font-size: 10px;")
        meta_row.addWidget(self.lbl_latency)
        meta_row.addStretch(1)
        layout.addLayout(meta_row)

        # Original speech line
        self.lbl_original = QLabel("")
        self.lbl_original.setWordWrap(True)
        self.lbl_original.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_original.setVisible(False)
        layout.addWidget(self.lbl_original)

        # Translated subtitle line
        self.lbl_translation = QLabel("正在识别并翻译...")
        self.lbl_translation.setWordWrap(True)
        self.lbl_translation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.lbl_translation)

        self.apply_style(font_size, show_original, is_pending=True)

    def apply_style(
        self,
        font_size: int,
        show_original: bool,
        is_pending: bool = False,
        is_error: bool = False,
    ) -> None:
        self._show_original = show_original
        orig_size = max(11, font_size - 3)
        self.lbl_original.setStyleSheet(
            f"color: #cbd5e1; font-size: {orig_size}px; line-height: 1.35;"
        )
        if is_error:
            self.lbl_translation.setStyleSheet(
                f"color: #fca5a5; font-size: {max(12, font_size - 2)}px;"
            )
        elif is_pending:
            self.lbl_translation.setStyleSheet(
                f"color: #94a3b8; font-size: {max(12, font_size - 2)}px; font-style: italic;"
            )
        else:
            self.lbl_translation.setStyleSheet(
                f"color: #ffffff; font-size: {font_size}px; font-weight: 600; line-height: 1.4;"
            )
        self.lbl_original.setVisible(show_original and bool(self.lbl_original.text().strip()))

    def set_result(self, res: TranslationResult, font_size: int, show_original: bool) -> None:
        if res.error:
            self.lbl_original.setText("")
            self.lbl_translation.setText(f"⚠ {res.error}")
            self.lbl_lang.setVisible(False)
            self.apply_style(font_size, show_original, is_error=True)
            return

        if res.source_lang:
            self.lbl_lang.setText(res.source_lang)
            self.lbl_lang.setVisible(True)
        if res.latency_ms > 0:
            self.lbl_latency.setText(f"{res.latency_ms / 1000:.1f}s")

        self.lbl_original.setText(res.original)
        self.lbl_translation.setText(res.translation)
        self.apply_style(font_size, show_original, is_pending=False)


class SubtitleOverlayWindow(QWidget):
    """Main floating translucent subtitle overlay window."""

    sig_level = pyqtSignal(str, float)
    sig_placeholder = pyqtSignal(int, str, str, str)
    sig_result = pyqtSignal(object)
    sig_error = pyqtSignal(str)

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self._cfg = cfg
        self._dragging = False
        self._drag_offset = QPoint()
        self._locked_compact = False
        self._click_through = False
        self._seq_counter = 0

        self._cards: dict[int, SubtitleCard] = {}
        self._card_order: list[int] = []
        self._history: list[TranscriptEntry] = []

        self._engine = TranslationEngine()
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="Translator")
        self._capture = AudioCaptureManager(
            on_segment=self._on_audio_segment,
            on_level=self._on_audio_level,
            on_error=self._on_capture_error,
        )

        self._icon = create_app_icon()
        self.setWindowIcon(self._icon)
        self.setWindowTitle("海外会议翻译助手")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(460, 140)

        self._build_ui()
        self._build_tray()
        self._connect_signals()
        self._restore_geometry()
        self._sync_quick_controls()
        self._update_background_style()

        # Prompt user to configure API key on very first launch
        if not self._has_active_api_key():
            QTimer.singleShot(350, self._show_first_run_hint)

    def _has_active_api_key(self) -> bool:
        if self._cfg.provider == "gemini":
            return bool(self._cfg.gemini_api_key.strip())
        return bool(self._cfg.openai_api_key.strip())

    def _show_first_run_hint(self) -> None:
        self._set_status("请先点击右上角 ⚙ 设置填写 API Key", "#fbbf24")

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.container = QFrame()
        self.container.setObjectName("MainContainer")
        outer.addWidget(self.container)

        main_layout = QVBoxLayout(self.container)
        main_layout.setContentsMargins(10, 8, 10, 6)
        main_layout.setSpacing(6)

        # Top control bar
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self.lbl_dot = QLabel("●")
        self.lbl_dot.setStyleSheet("color: #64748b; font-size: 13px;")
        bar.addWidget(self.lbl_dot)

        self.btn_toggle = QPushButton("▶ 开始翻译")
        self.btn_toggle.setObjectName("StartBtn")
        self.btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle.clicked.connect(self.toggle_capture)
        bar.addWidget(self.btn_toggle)

        self.cmb_quick_audio = QComboBox()
        self.cmb_quick_audio.setToolTip("快速切换采集通道（系统声音 / 麦克风 / 双通道）")
        for code, label in (
            ("system", "🔊 系统声音"),
            ("mic", "🎤 麦克风"),
            ("both", "🔊+🎤 双通道"),
        ):
            self.cmb_quick_audio.addItem(label, code)
        self.cmb_quick_audio.currentIndexChanged.connect(self._on_quick_audio_changed)
        bar.addWidget(self.cmb_quick_audio)

        self.cmb_quick_dir = QComboBox()
        self.cmb_quick_dir.setToolTip("快速切换翻译方向")
        for code, label in (
            ("auto_to_zh", "多语→中文"),
            ("bi_en_zh", "中英互译"),
            ("en_to_zh", "英→中"),
            ("zh_to_en", "中→英"),
            ("auto_to_en", "多语→英语"),
        ):
            self.cmb_quick_dir.addItem(label, code)
        self.cmb_quick_dir.currentIndexChanged.connect(self._on_quick_dir_changed)
        bar.addWidget(self.cmb_quick_dir)

        self.cmb_quick_engine = QComboBox()
        self.cmb_quick_engine.setToolTip("一键切换云端引擎（各自独立保存 Key 与代理，切换立即生效）")
        self.cmb_quick_engine.currentIndexChanged.connect(self._on_quick_engine_changed)
        bar.addWidget(self.cmb_quick_engine)

        self.lbl_status = QLabel("就绪 · 拖动顶栏可移动窗口")
        self.lbl_status.setStyleSheet("color: #94a3b8; font-size: 12px;")
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(self.lbl_status, 1)

        self.btn_bilingual = QPushButton("双语")
        self.btn_bilingual.setToolTip("切换双语对照 / 仅显示译文")
        self.btn_bilingual.setFixedWidth(48)
        self.btn_bilingual.clicked.connect(self._toggle_bilingual)
        bar.addWidget(self.btn_bilingual)

        self.btn_export = QPushButton("导出")
        self.btn_export.setToolTip("导出本场会议双语记录为 Markdown 文件（默认保存至 transcripts 目录）")
        self.btn_export.setFixedWidth(48)
        self.btn_export.clicked.connect(self.export_transcript)
        bar.addWidget(self.btn_export)

        self.btn_clear = QPushButton("清屏")
        self.btn_clear.setToolTip("清空当前屏幕上的字幕与上下文")
        self.btn_clear.setFixedWidth(48)
        self.btn_clear.clicked.connect(self.clear_subtitles)
        bar.addWidget(self.btn_clear)

        self.btn_lock = QPushButton("🔓")
        self.btn_lock.setToolTip("切换紧凑锁定模式（隐藏多余按钮、防止误拖动）")
        self.btn_lock.setFixedWidth(34)
        self.btn_lock.clicked.connect(self._toggle_compact_lock)
        bar.addWidget(self.btn_lock)

        self.btn_settings = QPushButton("⚙")
        self.btn_settings.setToolTip("打开设置（API Key、音频设备、术语库、字体与透明度）")
        self.btn_settings.setFixedWidth(34)
        self.btn_settings.clicked.connect(self.open_settings)
        bar.addWidget(self.btn_settings)

        self.btn_min = QPushButton("—")
        self.btn_min.setToolTip("隐藏到系统托盘")
        self.btn_min.setFixedWidth(30)
        self.btn_min.clicked.connect(self.hide_to_tray)
        bar.addWidget(self.btn_min)

        self.btn_close = QPushButton("✕")
        self.btn_close.setToolTip("退出程序")
        self.btn_close.setFixedWidth(30)
        self.btn_close.clicked.connect(self.quit_app)
        bar.addWidget(self.btn_close)

        main_layout.addLayout(bar)

        # Live audio level bar
        self.level_bar = LevelMeterBar()
        main_layout.addWidget(self.level_bar)

        # Scrollable subtitle feed
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            """
            QScrollArea { background: transparent; border: none; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
                margin: 2px 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(148, 163, 184, 0.35);
                border-radius: 3px;
                min-height: 24px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            """
        )

        self.feed_widget = QWidget()
        self.feed_layout = QVBoxLayout(self.feed_widget)
        self.feed_layout.setContentsMargins(0, 2, 4, 2)
        self.feed_layout.setSpacing(6)

        self.lbl_empty = QLabel(
            "点击上方「▶ 开始翻译」即可实时捕获会议语音并翻译。\n"
            "• 线上会议（Zoom / Teams / Meet）：选择「🔊 系统声音」\n"
            "• 线下会议（面对面交流）：选择「🎤 麦克风」\n"
            "• 同时翻译对方与自己：选择「🔊+🎤 双通道」"
        )
        self.lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_empty.setStyleSheet("color: #94a3b8; font-size: 13px; padding: 24px 14px;")
        self.feed_layout.addWidget(self.lbl_empty)
        self.feed_layout.addStretch(1)

        self.scroll.setWidget(self.feed_widget)
        main_layout.addWidget(self.scroll, 1)

        # Bottom subtle resize grip row
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        self.lbl_hint = QLabel("右下角可拖拽调整悬浮窗大小 · 托盘图标支持开启鼠标穿透")
        self.lbl_hint.setStyleSheet("color: rgba(148, 163, 184, 0.55); font-size: 10px;")
        grip_row.addWidget(self.lbl_hint)
        grip_row.addStretch(1)
        self.size_grip = QSizeGrip(self)
        self.size_grip.setFixedSize(14, 14)
        grip_row.addWidget(self.size_grip)
        main_layout.addLayout(grip_row)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._icon, self)
        self.tray.setToolTip("海外会议翻译助手")

        menu = QMenu()
        self.act_show = QAction("显示/置顶悬浮窗", self)
        self.act_show.triggered.connect(self._show_from_tray)
        menu.addAction(self.act_show)

        self.act_toggle_run = QAction("开始 / 停止翻译", self)
        self.act_toggle_run.triggered.connect(self.toggle_capture)
        menu.addAction(self.act_toggle_run)

        self.act_click_through = QAction("鼠标穿透模式（只看字幕不挡点击）", self)
        self.act_click_through.setCheckable(True)
        self.act_click_through.triggered.connect(self._toggle_click_through)
        menu.addAction(self.act_click_through)

        menu.addSeparator()

        act_export = QAction("导出会议字幕记录 (.md)", self)
        act_export.triggered.connect(self.export_transcript)
        menu.addAction(act_export)

        act_settings = QAction("设置...", self)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)

        menu.addSeparator()

        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _connect_signals(self) -> None:
        self.sig_level.connect(self._handle_level_ui)
        self.sig_placeholder.connect(self._handle_placeholder_ui)
        self.sig_result.connect(self._handle_result_ui)
        self.sig_error.connect(self._handle_error_ui)

    def _restore_geometry(self) -> None:
        w = max(460, int(self._cfg.window_width))
        h = max(140, int(self._cfg.window_height))
        self.resize(w, h)

        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            if self._cfg.window_x >= 0 and self._cfg.window_y >= 0:
                x = max(geo.left(), min(self._cfg.window_x, geo.right() - 120))
                y = max(geo.top(), min(self._cfg.window_y, geo.bottom() - 80))
                self.move(x, y)
            else:
                # Default: bottom-center of screen like classic live subtitles
                x = geo.left() + (geo.width() - w) // 2
                y = geo.bottom() - h - 36
                self.move(x, max(geo.top(), y))

    def _save_geometry(self) -> None:
        pos = self.pos()
        self._cfg.window_x = pos.x()
        self._cfg.window_y = pos.y()
        self._cfg.window_width = self.width()
        self._cfg.window_height = self.height()
        self._cfg.save()

    def _update_background_style(self) -> None:
        alpha = max(85, min(250, int(round(self._cfg.window_opacity * 255))))
        card_alpha = max(60, min(220, int(alpha * 0.72)))
        self.setStyleSheet(
            f"""
            QFrame#MainContainer {{
                background-color: rgba(15, 19, 28, {alpha});
                border: 1px solid rgba(148, 163, 184, 0.28);
                border-radius: 12px;
                font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
            }}
            QFrame#SubtitleCard {{
                background-color: rgba(30, 38, 54, {card_alpha});
                border: 1px solid rgba(148, 163, 184, 0.16);
                border-radius: 8px;
            }}
            QPushButton {{
                background-color: rgba(51, 65, 85, 0.65);
                color: #e2e8f0;
                border: 1px solid rgba(148, 163, 184, 0.22);
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: rgba(71, 85, 105, 0.85);
            }}
            QPushButton#StartBtn {{
                background-color: #2563eb;
                border: 1px solid #3b82f6;
                color: #ffffff;
                font-weight: bold;
                padding: 4px 12px;
            }}
            QPushButton#StartBtn:hover {{
                background-color: #3b82f6;
            }}
            QPushButton#StopBtn {{
                background-color: #dc2626;
                border: 1px solid #ef4444;
                color: #ffffff;
                font-weight: bold;
                padding: 4px 12px;
            }}
            QPushButton#StopBtn:hover {{
                background-color: #ef4444;
            }}
            QComboBox {{
                background-color: rgba(30, 41, 59, 0.85);
                color: #e2e8f0;
                border: 1px solid rgba(148, 163, 184, 0.25);
                border-radius: 6px;
                padding: 3px 8px;
                font-size: 12px;
            }}
            QComboBox QAbstractItemView {{
                background-color: #161b26;
                color: #eef3fc;
                selection-background-color: #2563eb;
            }}
            """
        )
        # Re-apply typography to existing cards
        for card in self._cards.values():
            if card.lbl_translation.text().startswith("⚠"):
                card.apply_style(self._cfg.font_size, self._cfg.show_original, is_error=True)
            elif card.lbl_translation.text() == "正在识别并翻译...":
                card.apply_style(self._cfg.font_size, self._cfg.show_original, is_pending=True)
            else:
                card.apply_style(self._cfg.font_size, self._cfg.show_original, is_pending=False)

    def _sync_quick_controls(self) -> None:
        self.cmb_quick_audio.blockSignals(True)
        idx = self.cmb_quick_audio.findData(self._cfg.audio_mode)
        if idx >= 0:
            self.cmb_quick_audio.setCurrentIndex(idx)
        self.cmb_quick_audio.blockSignals(False)

        self.cmb_quick_dir.blockSignals(True)
        idx = self.cmb_quick_dir.findData(self._cfg.direction)
        if idx >= 0:
            self.cmb_quick_dir.setCurrentIndex(idx)
        self.cmb_quick_dir.blockSignals(False)

        self.cmb_quick_engine.blockSignals(True)
        self.cmb_quick_engine.clear()
        g_label = self._cfg.engine_quick_label("gemini")
        if not self._cfg.gemini_api_key.strip():
            g_label += " (未配)"
        self.cmb_quick_engine.addItem(g_label, "gemini")

        if (
            not self._cfg.openai_api_key.strip()
            and self._cfg.openai_base_url.strip() == "https://api.openai.com/v1"
        ):
            o_label = "🇨🇳 国内/兼容 (去配置)"
        else:
            o_label = self._cfg.engine_quick_label("openai")
            if not self._cfg.openai_api_key.strip():
                o_label += " (未配)"
        self.cmb_quick_engine.addItem(o_label, "openai")

        if self._cfg.provider == "openai_audio":
            self.cmb_quick_engine.addItem(
                self._cfg.engine_quick_label("openai_audio"), "openai_audio"
            )

        idx = self.cmb_quick_engine.findData(self._cfg.provider)
        if idx >= 0:
            self.cmb_quick_engine.setCurrentIndex(idx)
        self.cmb_quick_engine.blockSignals(False)

        self.btn_bilingual.setText("双语" if self._cfg.show_original else "极简")
        self.level_bar.set_mode(self._cfg.audio_mode)

    def _set_status(self, text: str, dot_color: str | None = None) -> None:
        self.lbl_status.setText(text)
        if dot_color:
            self.lbl_dot.setStyleSheet(f"color: {dot_color}; font-size: 13px;")

    # --- Window dragging ---
    def mousePressEvent(self, event) -> None:
        if not self._locked_compact and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and not self._locked_compact:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._save_geometry()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # --- Quick controls ---
    def _on_quick_audio_changed(self) -> None:
        new_mode = str(self.cmb_quick_audio.currentData() or "system")
        if new_mode == self._cfg.audio_mode:
            return
        self._cfg.audio_mode = new_mode
        self._cfg.save()
        self.level_bar.set_mode(new_mode)
        if self._capture.is_running:
            self.stop_capture()
            self.start_capture()

    def _on_quick_dir_changed(self) -> None:
        new_dir = str(self.cmb_quick_dir.currentData() or "auto_to_zh")
        self._cfg.direction = new_dir
        self._cfg.save()

    def _on_quick_engine_changed(self) -> None:
        new_provider = str(self.cmb_quick_engine.currentData() or "gemini")
        if new_provider == self._cfg.provider:
            return

        has_key = (
            bool(self._cfg.gemini_api_key.strip())
            if new_provider == "gemini"
            else bool(self._cfg.openai_api_key.strip())
        )
        if not has_key:
            reply = QMessageBox.question(
                self,
                "尚未配置该引擎",
                "该引擎还没有填写 API Key，是否现在打开设置完成一次性配置？\n"
                "（Gemini 与国内/兼容接口的 Key 和代理相互独立保存，配好后即可一键秒切）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.open_settings(initial_provider=new_provider)
            self._sync_quick_controls()
            return

        self._cfg.provider = new_provider
        self._cfg.http_proxy = self._cfg.active_proxy()
        self._cfg.save()
        self._set_status(
            f"已切换：{self._cfg.engine_quick_label()}",
            "#22c55e" if self._capture.is_running else "#64748b",
        )

    def _toggle_bilingual(self) -> None:
        self._cfg.show_original = not self._cfg.show_original
        self._cfg.save()
        self.btn_bilingual.setText("双语" if self._cfg.show_original else "极简")
        self._update_background_style()

    def _toggle_compact_lock(self) -> None:
        self._locked_compact = not self._locked_compact
        self.btn_lock.setText("🔒" if self._locked_compact else "🔓")
        show_extra = not self._locked_compact
        self.cmb_quick_audio.setVisible(show_extra)
        self.cmb_quick_dir.setVisible(show_extra)
        self.cmb_quick_engine.setVisible(show_extra)
        self.btn_bilingual.setVisible(show_extra)
        self.btn_export.setVisible(show_extra)
        self.btn_clear.setVisible(show_extra)
        self.btn_settings.setVisible(show_extra)
        self.lbl_hint.setVisible(show_extra)
        self.size_grip.setVisible(show_extra)

    def _toggle_click_through(self, checked: bool) -> None:
        self._click_through = checked
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, checked)
        self.show()
        if checked:
            self.tray.showMessage(
                "已开启鼠标穿透",
                "现在可以直接点击字幕后方的会议窗口。右键点击任务栏托盘图标可随时取消穿透。",
                QSystemTrayIcon.MessageIcon.Information,
                3500,
            )

    def hide_to_tray(self) -> None:
        self.hide()
        self.tray.showMessage(
            "海外会议翻译助手仍在后台运行",
            "双击或右键任务栏右下角托盘图标可重新显示悬浮窗。",
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible():
                self.raise_()
                self.activateWindow()
            else:
                self._show_from_tray()

    # --- Audio capture & translation pipeline ---
    def toggle_capture(self) -> None:
        if self._capture.is_running:
            self.stop_capture()
        else:
            self.start_capture()

    def start_capture(self) -> None:
        if not self._has_active_api_key():
            QMessageBox.information(
                self,
                "需要配置 API Key",
                "首次使用请先在设置中填写 Gemini API Key 或 OpenAI 兼容接口 Key。",
            )
            self.open_settings()
            if not self._has_active_api_key():
                return

        try:
            active_devs = self._capture.start(self._cfg)
        except Exception as exc:
            self._set_status(f"启动失败: {exc}", "#ef4444")
            QMessageBox.warning(self, "无法启动音频采集", str(exc))
            return

        self.btn_toggle.setText("⏹ 停止翻译")
        self.btn_toggle.setObjectName("StopBtn")
        self._update_background_style()
        summary = " | ".join(active_devs)
        if len(summary) > 54:
            summary = summary[:51] + "..."
        self._set_status(f"监听中 · {summary}", "#22c55e")

    def stop_capture(self) -> None:
        self._capture.stop()
        self.level_bar.reset()
        self.btn_toggle.setText("▶ 开始翻译")
        self.btn_toggle.setObjectName("StartBtn")
        self._update_background_style()
        self._set_status("已停止", "#64748b")

    # Background thread callbacks -> Qt signals
    def _on_audio_level(self, channel_id: str, rms: float) -> None:
        self.sig_level.emit(channel_id, rms)

    def _on_capture_error(self, message: str) -> None:
        self.sig_error.emit(message)

    def _on_audio_segment(self, segment: AudioSegment) -> None:
        self._seq_counter += 1
        seq_id = self._seq_counter
        self.sig_placeholder.emit(
            seq_id,
            segment.timestamp,
            segment.channel_id,
            segment.channel_label,
        )
        cfg_snapshot = self._cfg
        self._executor.submit(self._run_translate_task, seq_id, segment, cfg_snapshot)

    def _run_translate_task(
        self, seq_id: int, segment: AudioSegment, cfg: AppConfig
    ) -> None:
        result = self._engine.translate_segment(seq_id, segment, cfg)
        self.sig_result.emit(result)

    # GUI thread slots
    def _handle_level_ui(self, channel_id: str, rms: float) -> None:
        self.level_bar.set_level(channel_id, rms)

    def _handle_error_ui(self, message: str) -> None:
        self._set_status(f"异常: {message}", "#ef4444")

    def _is_near_bottom(self) -> bool:
        bar = self.scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - 48

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        QTimer.singleShot(20, lambda: bar.setValue(bar.maximum()))

    def _handle_placeholder_ui(
        self, seq_id: int, timestamp: str, channel_id: str, channel_label: str
    ) -> None:
        if self.lbl_empty.isVisible():
            self.lbl_empty.setVisible(False)

        was_bottom = self._is_near_bottom()
        card = SubtitleCard(
            timestamp=timestamp,
            channel_id=channel_id,
            channel_label=channel_label,
            font_size=self._cfg.font_size,
            show_original=self._cfg.show_original,
            parent=self.feed_widget,
        )
        # Insert right before bottom stretch
        insert_idx = max(0, self.feed_layout.count() - 1)
        self.feed_layout.insertWidget(insert_idx, card)
        self._cards[seq_id] = card
        self._card_order.append(seq_id)

        # Trim oldest cards beyond max_visible_items
        while len(self._card_order) > max(5, int(self._cfg.max_visible_items)):
            old_id = self._card_order.pop(0)
            old_card = self._cards.pop(old_id, None)
            if old_card is not None:
                self.feed_layout.removeWidget(old_card)
                old_card.deleteLater()

        if was_bottom:
            self._scroll_to_bottom()

    def _handle_result_ui(self, result: TranslationResult) -> None:
        card = self._cards.get(result.seq_id)
        was_bottom = self._is_near_bottom()

        if not result.speech_detected and not result.error:
            # Remove placeholder for silence / background noise
            if card is not None:
                self._cards.pop(result.seq_id, None)
                if result.seq_id in self._card_order:
                    self._card_order.remove(result.seq_id)
                self.feed_layout.removeWidget(card)
                card.deleteLater()
            return

        if card is not None:
            card.set_result(result, self._cfg.font_size, self._cfg.show_original)

        if not result.error and result.translation:
            self._history.append(
                TranscriptEntry(
                    seq_id=result.seq_id,
                    timestamp=result.timestamp,
                    channel_id=result.channel_id,
                    channel_label=result.channel_label,
                    source_lang=result.source_lang,
                    original=result.original,
                    translation=result.translation,
                )
            )
            if self._capture.is_running:
                self._set_status(f"实时翻译中 · 最近延迟 {result.latency_ms / 1000:.1f}s", "#22c55e")
        elif result.error:
            self._set_status(result.error[:56], "#f59e0b")

        if was_bottom:
            self._scroll_to_bottom()

    # --- Settings, Clear, Export, Quit ---
    def open_settings(
        self, _checked: bool = False, *, initial_provider: str | None = None
    ) -> None:
        was_running = self._capture.is_running
        old_audio_sig = (
            self._cfg.audio_mode,
            self._cfg.system_device_index,
            self._cfg.system_device_name,
            self._cfg.mic_device_index,
            self._cfg.mic_device_name,
            self._cfg.vad_threshold,
            self._cfg.silence_duration,
            self._cfg.max_chunk_duration,
            self._cfg.min_speech_duration,
        )

        dlg = SettingsDialog(self._cfg, self, initial_provider=initial_provider)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._cfg = dlg.get_config()
            self._sync_quick_controls()
            self._update_background_style()

            new_audio_sig = (
                self._cfg.audio_mode,
                self._cfg.system_device_index,
                self._cfg.system_device_name,
                self._cfg.mic_device_index,
                self._cfg.mic_device_name,
                self._cfg.vad_threshold,
                self._cfg.silence_duration,
                self._cfg.max_chunk_duration,
                self._cfg.min_speech_duration,
            )
            if was_running and new_audio_sig != old_audio_sig:
                self.stop_capture()
                self.start_capture()

    def clear_subtitles(self) -> None:
        for card in self._cards.values():
            self.feed_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        self._card_order.clear()
        self._engine.clear_context()
        self.lbl_empty.setVisible(True)

    def export_transcript(self) -> None:
        if not self._history:
            QMessageBox.information(self, "导出会议记录", "当前会话还没有可导出的翻译记录。")
            return

        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        default_name = f"meeting_transcript_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        default_path = TRANSCRIPTS_DIR / default_name

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出会议双语记录",
            str(default_path),
            "Markdown 文件 (*.md);;纯文本文件 (*.txt)",
        )
        if not file_path:
            return

        lines = [
            f"# 会议实时字幕翻译记录 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
            "",
        ]
        for item in self._history:
            lang_tag = f" [{item.source_lang}]" if item.source_lang else ""
            lines.append(f"### `{item.timestamp}` {item.channel_label}{lang_tag}")
            if item.original:
                lines.append(f"- **原文**: {item.original}")
            lines.append(f"- **译文**: {item.translation}")
            lines.append("")

        Path(file_path).write_text("\n".join(lines), encoding="utf-8")
        QMessageBox.information(self, "导出成功", f"会议记录已保存至：\n{file_path}")

    def quit_app(self) -> None:
        self.stop_capture()
        self._save_geometry()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        self.quit_app()
        event.accept()
