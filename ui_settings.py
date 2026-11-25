"""Settings dialog for Meeting Subtitle Translator."""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from audio_capture import AudioDeviceInfo, list_audio_devices
from config import (
    AUDIO_MODES,
    COMPAT_QUICK_PRESETS,
    PROVIDER_PRESETS,
    TRANSLATION_DIRECTIONS,
    AppConfig,
)
from translator import test_api_connection


class _ConnectionTestThread(QThread):
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, cfg: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg

    def run(self) -> None:
        ok, msg = test_api_connection(self._cfg)
        self.finished_signal.emit(ok, msg)


class SettingsDialog(QDialog):
    """Dark-themed tabbed settings dialog."""

    def __init__(
        self,
        cfg: AppConfig,
        parent: QWidget | None = None,
        initial_provider: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = replace(cfg)
        self._initial_provider = initial_provider
        self._loopbacks: list[AudioDeviceInfo] = []
        self._mics: list[AudioDeviceInfo] = []
        self._test_thread: _ConnectionTestThread | None = None

        self.setWindowTitle("海外会议翻译助手 · 设置")
        self.setMinimumSize(620, 560)
        self.setStyleSheet(self._stylesheet())

        self._build_ui()
        self._refresh_devices()
        self._load_values()
        if self._initial_provider:
            idx = self.cmb_provider.findData(self._initial_provider)
            if idx >= 0:
                self.cmb_provider.setCurrentIndex(idx)
            # If opening OpenAI/compatible for the first time with no key, pre-fill SiliconFlow preset
            if (
                self._initial_provider == "openai"
                and not self.ed_openai_key.text().strip()
                and self.ed_openai_base.text().strip() == "https://api.openai.com/v1"
            ):
                sf_idx = self.cmb_quick_preset.findData("硅基流动 SiliconFlow（国内直连）")
                if sf_idx >= 0:
                    self.cmb_quick_preset.setCurrentIndex(sf_idx)

    @staticmethod
    def _stylesheet() -> str:
        return """
        QDialog {
            background-color: #161922;
            color: #e6ebf5;
            font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
            font-size: 13px;
        }
        QTabWidget::pane {
            border: 1px solid #2a3142;
            border-radius: 8px;
            background-color: #1b202c;
            padding: 8px;
        }
        QTabBar::tab {
            background-color: #161922;
            color: #9aa7bd;
            padding: 8px 18px;
            margin-right: 4px;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            border: 1px solid #262d3d;
            border-bottom: none;
        }
        QTabBar::tab:selected {
            background-color: #1b202c;
            color: #ffffff;
            font-weight: bold;
        }
        QGroupBox {
            border: 1px solid #2b3346;
            border-radius: 6px;
            margin-top: 10px;
            padding-top: 12px;
            font-weight: bold;
            color: #8ab4f8;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 6px;
        }
        QLabel {
            color: #d6deed;
        }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
            background-color: #11141c;
            color: #eef3fc;
            border: 1px solid #30394e;
            border-radius: 5px;
            padding: 5px 8px;
        }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {
            border: 1px solid #5c9eff;
        }
        QComboBox QAbstractItemView {
            background-color: #161a24;
            color: #eef3fc;
            selection-background-color: #2f5aa8;
        }
        QPushButton {
            background-color: #273044;
            color: #eef3fc;
            border: 1px solid #394661;
            border-radius: 6px;
            padding: 6px 14px;
        }
        QPushButton:hover {
            background-color: #323e57;
        }
        QPushButton#PrimaryBtn {
            background-color: #2d6cdf;
            border: 1px solid #4c86f0;
            color: #ffffff;
            font-weight: bold;
        }
        QPushButton#PrimaryBtn:hover {
            background-color: #3b7bf2;
        }
        QCheckBox {
            color: #d6deed;
            spacing: 6px;
        }
        """

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_api_tab(), "API 与引擎")
        self.tabs.addTab(self._build_audio_tab(), "音频与检测")
        self.tabs.addTab(self._build_display_tab(), "翻译与显示")
        root.addWidget(self.tabs)

        # Bottom buttons
        btn_row = QHBoxLayout()
        self.lbl_test_status = QLabel("")
        self.lbl_test_status.setWordWrap(True)
        self.lbl_test_status.setStyleSheet("color: #9aa7bd; font-size: 12px;")
        btn_row.addWidget(self.lbl_test_status, 1)

        self.btn_test = QPushButton("测试 API 连接")
        self.btn_test.clicked.connect(self._on_test_connection)
        btn_row.addWidget(self.btn_test)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        btn_save = QPushButton("保存并应用")
        btn_save.setObjectName("PrimaryBtn")
        btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(btn_save)

        root.addLayout(btn_row)

    def _build_api_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        # Provider selector
        top_form = QFormLayout()
        self.cmb_provider = QComboBox()
        for key, meta in PROVIDER_PRESETS.items():
            self.cmb_provider.addItem(meta["label"], key)
        self.cmb_provider.currentIndexChanged.connect(self._update_provider_visibility)
        top_form.addRow("云端引擎:", self.cmb_provider)
        layout.addLayout(top_form)

        # Gemini Group
        self.grp_gemini = QGroupBox("Google Gemini API 配置（音频一步直出翻译，推荐）")
        g_form = QFormLayout(self.grp_gemini)

        key_row = QHBoxLayout()
        self.ed_gemini_key = QLineEdit()
        self.ed_gemini_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_gemini_key.setPlaceholderText("粘贴你的 Gemini API Key（AIza...）")
        btn_eye_g = QPushButton("显示")
        btn_eye_g.setFixedWidth(56)
        btn_eye_g.clicked.connect(lambda: self._toggle_echo(self.ed_gemini_key, btn_eye_g))
        key_row.addWidget(self.ed_gemini_key, 1)
        key_row.addWidget(btn_eye_g)
        g_form.addRow("API Key:", key_row)

        self.ed_gemini_base = QLineEdit()
        self.ed_gemini_base.setPlaceholderText("https://generativelanguage.googleapis.com")
        g_form.addRow("Base URL:", self.ed_gemini_base)

        self.cmb_gemini_model = QComboBox()
        self.cmb_gemini_model.setEditable(True)
        self.cmb_gemini_model.addItems(
            ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
        )
        g_form.addRow("模型名称:", self.cmb_gemini_model)

        self.ed_gemini_proxy = QLineEdit()
        self.ed_gemini_proxy.setPlaceholderText("例如 http://127.0.0.1:10808（仅对 Gemini 生效，留空直连）")
        g_form.addRow("独立 HTTP 代理:", self.ed_gemini_proxy)
        layout.addWidget(self.grp_gemini)

        # OpenAI / Compatible Group
        self.grp_openai = QGroupBox("OpenAI / 兼容接口配置（支持 OpenAI、Groq、硅基流动、中转站）")
        o_form = QFormLayout(self.grp_openai)

        preset_row = QHBoxLayout()
        self.cmb_quick_preset = QComboBox()
        self.cmb_quick_preset.addItem("选择常用预设快速填充...", "")
        for pname in COMPAT_QUICK_PRESETS:
            self.cmb_quick_preset.addItem(pname, pname)
        self.cmb_quick_preset.currentIndexChanged.connect(self._apply_quick_preset)
        preset_row.addWidget(self.cmb_quick_preset)
        o_form.addRow("快捷预设:", preset_row)

        okey_row = QHBoxLayout()
        self.ed_openai_key = QLineEdit()
        self.ed_openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_openai_key.setPlaceholderText("粘贴 API Key（sk-... / gsk_...）")
        btn_eye_o = QPushButton("显示")
        btn_eye_o.setFixedWidth(56)
        btn_eye_o.clicked.connect(lambda: self._toggle_echo(self.ed_openai_key, btn_eye_o))
        okey_row.addWidget(self.ed_openai_key, 1)
        okey_row.addWidget(btn_eye_o)
        o_form.addRow("API Key:", okey_row)

        self.ed_openai_base = QLineEdit()
        self.ed_openai_base.setPlaceholderText("https://api.openai.com/v1")
        o_form.addRow("Base URL:", self.ed_openai_base)

        self.ed_openai_stt = QLineEdit()
        self.ed_openai_stt.setPlaceholderText("gpt-4o-mini-transcribe / whisper-1 / whisper-large-v3-turbo")
        self.lbl_stt_model = QLabel("语音识别模型:")
        o_form.addRow(self.lbl_stt_model, self.ed_openai_stt)

        self.ed_openai_llm = QLineEdit()
        self.ed_openai_llm.setPlaceholderText("gpt-4o-mini / llama-3.3-70b-versatile / Qwen2.5")
        self.lbl_llm_model = QLabel("文本翻译模型:")
        o_form.addRow(self.lbl_llm_model, self.ed_openai_llm)

        self.ed_openai_audio = QLineEdit()
        self.ed_openai_audio.setPlaceholderText("gpt-4o-mini-audio-preview")
        self.lbl_audio_model = QLabel("Audio 直出模型:")
        o_form.addRow(self.lbl_audio_model, self.ed_openai_audio)

        self.ed_openai_proxy = QLineEdit()
        self.ed_openai_proxy.setPlaceholderText("国内直连（如硅基流动）请留空；海外接口再填代理")
        o_form.addRow("独立 HTTP 代理:", self.ed_openai_proxy)

        # Optional separate LLM (e.g. Groq Whisper STT + DeepSeek LLM)
        self.chk_sep_llm = QCheckBox("翻译 LLM 使用独立接口（例如：Groq 语音识别 + DeepSeek 翻译）")
        self.chk_sep_llm.toggled.connect(self._update_separate_llm_visibility)
        o_form.addRow("", self.chk_sep_llm)

        self.sep_llm_box = QWidget()
        s_form = QFormLayout(self.sep_llm_box)
        s_form.setContentsMargins(0, 4, 0, 0)
        self.ed_sep_key = QLineEdit()
        self.ed_sep_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_sep_key.setPlaceholderText("独立翻译 LLM 的 API Key")
        self.ed_sep_base = QLineEdit()
        self.ed_sep_base.setPlaceholderText("https://api.deepseek.com/v1")
        self.ed_sep_model = QLineEdit()
        self.ed_sep_model.setPlaceholderText("deepseek-chat")
        s_form.addRow("LLM Key:", self.ed_sep_key)
        s_form.addRow("LLM Base URL:", self.ed_sep_base)
        s_form.addRow("LLM 模型:", self.ed_sep_model)
        o_form.addRow(self.sep_llm_box)

        layout.addWidget(self.grp_openai)

        # Network timeout & dual-profile tip
        grp_net = QGroupBox("通用请求设置")
        n_form = QFormLayout(grp_net)

        self.spn_timeout = QSpinBox()
        self.spn_timeout.setRange(5, 60)
        self.spn_timeout.setSuffix(" 秒")
        n_form.addRow("超时时间:", self.spn_timeout)

        net_tip = QLabel(
            "提示：Gemini 与 OpenAI/国内兼容接口的 API Key 和代理相互独立保存。"
            "两套都填好后，可在悬浮窗顶栏一键秒切（例如不开代理切硅基流动，开代理切 Gemini），无需重复配置。"
        )
        net_tip.setWordWrap(True)
        net_tip.setStyleSheet("color: #8f9bb3; font-size: 12px;")
        n_form.addRow("", net_tip)
        layout.addWidget(grp_net)

        layout.addStretch(1)
        return page

    def _build_audio_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        grp_dev = QGroupBox("音频采集通道")
        d_form = QFormLayout(grp_dev)

        self.cmb_audio_mode = QComboBox()
        for code, label in AUDIO_MODES:
            self.cmb_audio_mode.addItem(label, code)
        d_form.addRow("采集模式:", self.cmb_audio_mode)

        sys_row = QHBoxLayout()
        self.cmb_sys_dev = QComboBox()
        btn_refresh = QPushButton("刷新设备")
        btn_refresh.setFixedWidth(84)
        btn_refresh.clicked.connect(self._refresh_devices)
        sys_row.addWidget(self.cmb_sys_dev, 1)
        sys_row.addWidget(btn_refresh)
        d_form.addRow("系统声音通道:", sys_row)

        self.cmb_mic_dev = QComboBox()
        d_form.addRow("麦克风通道:", self.cmb_mic_dev)

        tip = QLabel(
            "提示：线上会议选「仅系统声音」可直接抓取对方声音（戴耳机也可）；\n"
            "线下与国外客户面对面开会选「仅麦克风」；需要同时记录双方对话选「双通道」。"
        )
        tip.setStyleSheet("color: #8f9bb3; font-size: 12px;")
        tip.setWordWrap(True)
        d_form.addRow("", tip)
        layout.addWidget(grp_dev)

        grp_vad = QGroupBox("智能断句与灵敏度（VAD）")
        v_form = QFormLayout(grp_vad)

        vad_row = QHBoxLayout()
        self.sld_vad = QSlider(Qt.Orientation.Horizontal)
        self.sld_vad.setRange(2, 60)  # maps to 0.002 - 0.060
        self.lbl_vad_val = QLabel("0.012")
        self.lbl_vad_val.setFixedWidth(48)
        self.sld_vad.valueChanged.connect(lambda v: self.lbl_vad_val.setText(f"{v / 1000:.3f}"))
        vad_row.addWidget(self.sld_vad, 1)
        vad_row.addWidget(self.lbl_vad_val)
        v_form.addRow("触发阈值(越低越敏感):", vad_row)

        self.spn_silence = QDoubleSpinBox()
        self.spn_silence.setRange(0.4, 2.5)
        self.spn_silence.setSingleStep(0.1)
        self.spn_silence.setSuffix(" 秒")
        v_form.addRow("停顿断句时长:", self.spn_silence)

        self.spn_max_chunk = QDoubleSpinBox()
        self.spn_max_chunk.setRange(3.0, 20.0)
        self.spn_max_chunk.setSingleStep(0.5)
        self.spn_max_chunk.setSuffix(" 秒")
        v_form.addRow("连续说话最大分段:", self.spn_max_chunk)

        self.spn_min_speech = QDoubleSpinBox()
        self.spn_min_speech.setRange(0.2, 2.0)
        self.spn_min_speech.setSingleStep(0.05)
        self.spn_min_speech.setSuffix(" 秒")
        v_form.addRow("最短有效语音:", self.spn_min_speech)

        layout.addWidget(grp_vad)
        layout.addStretch(1)
        return page

    def _build_display_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        grp_trans = QGroupBox("翻译语言与专业术语")
        t_form = QFormLayout(grp_trans)

        self.cmb_direction = QComboBox()
        for code, label in TRANSLATION_DIRECTIONS:
            self.cmb_direction.addItem(label, code)
        t_form.addRow("翻译方向:", self.cmb_direction)

        self.spn_ctx_turns = QSpinBox()
        self.spn_ctx_turns.setRange(0, 8)
        self.spn_ctx_turns.setSuffix(" 句")
        t_form.addRow("上下文参考句数:", self.spn_ctx_turns)

        self.txt_glossary = QPlainTextEdit()
        self.txt_glossary.setPlaceholderText(
            "可选：输入会议背景、客户名称或行业术语，显著提升翻译准确度。\n"
            "例如：我们是 SaaS 软件供应商，常见术语：ARR, SLA, POC, FOB, MOQ, SKU..."
        )
        self.txt_glossary.setFixedHeight(96)
        t_form.addRow("行业术语/背景:", self.txt_glossary)
        layout.addWidget(grp_trans)

        grp_ui = QGroupBox("悬浮窗外观")
        u_form = QFormLayout(grp_ui)

        self.chk_show_orig = QCheckBox("双语对照显示（同时显示外语原文与翻译结果）")
        u_form.addRow("", self.chk_show_orig)

        self.spn_font_size = QSpinBox()
        self.spn_font_size.setRange(12, 30)
        self.spn_font_size.setSuffix(" px")
        u_form.addRow("译文字号:", self.spn_font_size)

        op_row = QHBoxLayout()
        self.sld_opacity = QSlider(Qt.Orientation.Horizontal)
        self.sld_opacity.setRange(35, 100)
        self.lbl_opacity = QLabel("88%")
        self.lbl_opacity.setFixedWidth(44)
        self.sld_opacity.valueChanged.connect(lambda v: self.lbl_opacity.setText(f"{v}%"))
        op_row.addWidget(self.sld_opacity, 1)
        op_row.addWidget(self.lbl_opacity)
        u_form.addRow("背景不透明度:", op_row)

        self.spn_max_items = QSpinBox()
        self.spn_max_items.setRange(5, 200)
        self.spn_max_items.setSuffix(" 条")
        u_form.addRow("窗口保留字幕数:", self.spn_max_items)

        layout.addWidget(grp_ui)
        layout.addStretch(1)
        return page

    @staticmethod
    def _toggle_echo(edit: QLineEdit, btn: QPushButton) -> None:
        if edit.echoMode() == QLineEdit.EchoMode.Password:
            edit.setEchoMode(QLineEdit.EchoMode.Normal)
            btn.setText("隐藏")
        else:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            btn.setText("显示")

    def _apply_quick_preset(self) -> None:
        name = self.cmb_quick_preset.currentData()
        if not name or name not in COMPAT_QUICK_PRESETS:
            return
        preset = COMPAT_QUICK_PRESETS[name]
        self.ed_openai_base.setText(preset["base_url"])
        self.ed_openai_stt.setText(preset["stt_model"])
        self.ed_openai_llm.setText(preset["llm_model"])
        if "siliconflow" in preset["base_url"].lower():
            self.ed_openai_proxy.setText("")

    def _update_provider_visibility(self) -> None:
        provider = self.cmb_provider.currentData()
        is_gemini = provider == "gemini"
        is_audio = provider == "openai_audio"

        self.grp_gemini.setVisible(is_gemini)
        self.grp_openai.setVisible(not is_gemini)

        self.lbl_stt_model.setVisible(not is_audio)
        self.ed_openai_stt.setVisible(not is_audio)
        self.lbl_llm_model.setVisible(not is_audio)
        self.ed_openai_llm.setVisible(not is_audio)
        self.chk_sep_llm.setVisible(not is_audio)
        self.lbl_audio_model.setVisible(is_audio)
        self.ed_openai_audio.setVisible(is_audio)

        self._update_separate_llm_visibility()

    def _update_separate_llm_visibility(self) -> None:
        provider = self.cmb_provider.currentData()
        show_sep = provider == "openai" and self.chk_sep_llm.isChecked()
        self.sep_llm_box.setVisible(show_sep)

    def _refresh_devices(self) -> None:
        prev_sys_idx = self.cmb_sys_dev.currentData() if self.cmb_sys_dev.count() else None
        prev_mic_idx = self.cmb_mic_dev.currentData() if self.cmb_mic_dev.count() else None

        self.cmb_sys_dev.clear()
        self.cmb_mic_dev.clear()
        self.cmb_sys_dev.addItem("自动跟随 Windows 默认播放设备（推荐）", (-1, ""))
        self.cmb_mic_dev.addItem("自动跟随 Windows 默认麦克风（推荐）", (-1, ""))

        try:
            self._loopbacks, self._mics = list_audio_devices()
            for dev in self._loopbacks:
                self.cmb_sys_dev.addItem(dev.display_name, (dev.index, dev.name))
            for dev in self._mics:
                self.cmb_mic_dev.addItem(dev.display_name, (dev.index, dev.name))
        except Exception as exc:
            QMessageBox.warning(self, "音频设备扫描提示", f"读取音频设备列表时出现异常：\n{exc}")

        if prev_sys_idx is not None:
            pos = self.cmb_sys_dev.findData(prev_sys_idx)
            if pos >= 0:
                self.cmb_sys_dev.setCurrentIndex(pos)
        if prev_mic_idx is not None:
            pos = self.cmb_mic_dev.findData(prev_mic_idx)
            if pos >= 0:
                self.cmb_mic_dev.setCurrentIndex(pos)

    def _select_device_combo(self, combo: QComboBox, saved_idx: int, saved_name: str) -> None:
        if saved_idx < 0 and not saved_name:
            combo.setCurrentIndex(0)
            return
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data and data[0] == saved_idx and (not saved_name or data[1] == saved_name):
                combo.setCurrentIndex(i)
                return
        if saved_name:
            for i in range(combo.count()):
                data = combo.itemData(i)
                if data and data[1] == saved_name:
                    combo.setCurrentIndex(i)
                    return
        combo.setCurrentIndex(0)

    def _load_values(self) -> None:
        c = self._cfg

        idx = self.cmb_provider.findData(c.provider)
        if idx >= 0:
            self.cmb_provider.setCurrentIndex(idx)

        self.ed_gemini_key.setText(c.gemini_api_key)
        self.ed_gemini_base.setText(c.gemini_base_url)
        self.cmb_gemini_model.setCurrentText(c.gemini_model)
        self.ed_gemini_proxy.setText(c.gemini_http_proxy)

        self.ed_openai_key.setText(c.openai_api_key)
        self.ed_openai_base.setText(c.openai_base_url)
        self.ed_openai_stt.setText(c.openai_stt_model)
        self.ed_openai_llm.setText(c.openai_llm_model)
        self.ed_openai_audio.setText(c.openai_audio_model)
        self.ed_openai_proxy.setText(c.openai_http_proxy)

        self.chk_sep_llm.setChecked(c.use_separate_llm)
        self.ed_sep_key.setText(c.separate_llm_api_key)
        self.ed_sep_base.setText(c.separate_llm_base_url)
        self.ed_sep_model.setText(c.separate_llm_model)

        self.spn_timeout.setValue(int(c.request_timeout))

        idx = self.cmb_audio_mode.findData(c.audio_mode)
        if idx >= 0:
            self.cmb_audio_mode.setCurrentIndex(idx)
        self._select_device_combo(self.cmb_sys_dev, c.system_device_index, c.system_device_name)
        self._select_device_combo(self.cmb_mic_dev, c.mic_device_index, c.mic_device_name)

        self.sld_vad.setValue(max(2, min(60, int(round(c.vad_threshold * 1000)))))
        self.spn_silence.setValue(float(c.silence_duration))
        self.spn_max_chunk.setValue(float(c.max_chunk_duration))
        self.spn_min_speech.setValue(float(c.min_speech_duration))

        idx = self.cmb_direction.findData(c.direction)
        if idx >= 0:
            self.cmb_direction.setCurrentIndex(idx)
        self.spn_ctx_turns.setValue(int(c.context_turns))
        self.txt_glossary.setPlainText(c.glossary)

        self.chk_show_orig.setChecked(bool(c.show_original))
        self.spn_font_size.setValue(int(c.font_size))
        self.sld_opacity.setValue(max(35, min(100, int(round(c.window_opacity * 100)))))
        self.spn_max_items.setValue(int(c.max_visible_items))

        self._update_provider_visibility()

    def build_config(self) -> AppConfig:
        c = replace(self._cfg)
        c.provider = str(self.cmb_provider.currentData() or "gemini")

        c.gemini_api_key = self.ed_gemini_key.text().strip()
        c.gemini_base_url = self.ed_gemini_base.text().strip() or "https://generativelanguage.googleapis.com"
        c.gemini_model = self.cmb_gemini_model.currentText().strip() or "gemini-3.8-flash"
        c.gemini_http_proxy = self.ed_gemini_proxy.text().strip()

        c.openai_api_key = self.ed_openai_key.text().strip()
        c.openai_base_url = self.ed_openai_base.text().strip() or "https://api.openai.com/v1"
        c.openai_stt_model = self.ed_openai_stt.text().strip() or "gpt-4o-mini-transcribe"
        c.openai_llm_model = self.ed_openai_llm.text().strip() or "gpt-4o-mini"
        c.openai_audio_model = self.ed_openai_audio.text().strip() or "gpt-4o-mini-audio-preview"
        c.openai_http_proxy = self.ed_openai_proxy.text().strip()

        c.use_separate_llm = self.chk_sep_llm.isChecked()
        c.separate_llm_api_key = self.ed_sep_key.text().strip()
        c.separate_llm_base_url = self.ed_sep_base.text().strip() or "https://api.deepseek.com/v1"
        c.separate_llm_model = self.ed_sep_model.text().strip() or "deepseek-chat"

        c.http_proxy = c.active_proxy()
        c.request_timeout = int(self.spn_timeout.value())

        c.audio_mode = str(self.cmb_audio_mode.currentData() or "system")
        sys_data = self.cmb_sys_dev.currentData() or (-1, "")
        c.system_device_index, c.system_device_name = int(sys_data[0]), str(sys_data[1])
        mic_data = self.cmb_mic_dev.currentData() or (-1, "")
        c.mic_device_index, c.mic_device_name = int(mic_data[0]), str(mic_data[1])

        c.vad_threshold = self.sld_vad.value() / 1000.0
        c.silence_duration = float(self.spn_silence.value())
        c.max_chunk_duration = float(self.spn_max_chunk.value())
        c.min_speech_duration = float(self.spn_min_speech.value())

        c.direction = str(self.cmb_direction.currentData() or "auto_to_zh")
        c.context_turns = int(self.spn_ctx_turns.value())
        c.glossary = self.txt_glossary.toPlainText().strip()

        c.show_original = self.chk_show_orig.isChecked()
        c.font_size = int(self.spn_font_size.value())
        c.window_opacity = self.sld_opacity.value() / 100.0
        c.max_visible_items = int(self.spn_max_items.value())
        return c

    def _on_test_connection(self) -> None:
        if self._test_thread is not None and self._test_thread.isRunning():
            return
        candidate = self.build_config()
        self.btn_test.setEnabled(False)
        self.lbl_test_status.setStyleSheet("color: #f6c343; font-size: 12px;")
        self.lbl_test_status.setText("正在测试连接...")

        self._test_thread = _ConnectionTestThread(candidate, None)
        self._test_thread.finished_signal.connect(self._on_test_finished)
        self._test_thread.finished.connect(self._test_thread.deleteLater)
        self._test_thread.start()

    def _on_test_finished(self, ok: bool, msg: str) -> None:
        self._test_thread = None
        if not self.isVisible():
            return
        self.btn_test.setEnabled(True)
        if ok:
            self.lbl_test_status.setStyleSheet("color: #4ade80; font-size: 12px;")
        else:
            self.lbl_test_status.setStyleSheet("color: #f87171; font-size: 12px;")
        self.lbl_test_status.setText(msg)

    def _on_save(self) -> None:
        self._cfg = self.build_config()
        self._cfg.save()
        self.accept()

    def get_config(self) -> AppConfig:
        return self._cfg
