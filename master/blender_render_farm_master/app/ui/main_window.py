from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Qt, QSize
from PySide6.QtGui import QBrush, QColor, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QLayout,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.blend_version import BlendVersionMetadata, blend_version_metadata_from_job, read_blend_version_metadata
from app.core.job_manager import JobManager, JobValidationError, WorkerPathValidationError
from app.core.models import normalize_progress_percent, parse_datetime
from app.core.runtime_metadata import load_runtime_metadata
from app.core.version_routing import (
    PREFER_HIGHER_MAJOR_WHEN_SAME_MAJOR_OLDER_POLICY,
    PREFER_SAME_MAJOR_ANY_VERSION_POLICY,
)
from app.services.logger import AppLogger
from app.services.persistence import PersistenceService
from app.core.settings import save_settings
from renderfarm_shared.i18n import LANGUAGE_NAMES, normalize_language, tr, translate_message
from renderfarm_shared.presentation import relative_age, resource_path
from renderfarm_shared.qt_layout import AdaptiveGrid, scrollable
from app.ui.widgets import (
    BlendFileLineEdit,
    LogViewerDialog,
    MetricCard,
    TextReportDialog,
    WorkerDialog,
    create_status_label,
    job_status_view,
    worker_status_state,
    worker_status_view,
)


class MainWindow(QMainWindow):
    def __init__(
        self,
        base_dir: Path,
        job_manager: Any,
        persistence: PersistenceService,
        logger: AppLogger,
        default_block_size: int,
        default_worker_port: int,
        poll_interval_ms: int,
    ) -> None:
        super().__init__()
        self.base_dir = base_dir
        self.job_manager = job_manager
        self.persistence = persistence
        self.logger = logger
        self.default_worker_port = default_worker_port
        self.mode = getattr(job_manager, "mode", "direct")
        self.settings = getattr(job_manager, "settings", None)
        self.language = normalize_language(getattr(self.settings, "language", "en"))
        self.runtime_metadata = load_runtime_metadata(base_dir)
        self.selected_worker_id: str | None = None
        self.selected_worker_job_id: str | None = None
        self.selected_queue_job_id: str | None = None
        self._last_snapshot: dict[str, Any] = {}
        self._all_workers: list[dict[str, Any]] = []
        self._visible_worker_ids: set[str] = set()
        self._master_log_source = ""
        self._local_blend_version: BlendVersionMetadata | None = None
        self._local_blend_version_status: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._tick_future: Future | None = None
        self._discovery_executor = ThreadPoolExecutor(max_workers=1)
        self._discovery_future: Future | None = None

        self.setWindowTitle("Blender Render Farm Master")
        self.setMinimumSize(760, 440)
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1672, available.width() - 40), min(940, available.height() - 50))

        self._build_ui(default_block_size)
        self._configure_mode_ui()
        self._load_initial_snapshot()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._on_poll_timer)
        self.poll_timer.start(poll_interval_ms)

    def _t(self, text: str, **values: Any) -> str:
        return tr(text, self.language, **values)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.poll_timer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._discovery_executor.shutdown(wait=False, cancel_futures=True)
        if hasattr(self.job_manager, "close"):
            self.job_manager.close()
        super().closeEvent(event)

    def _build_header_strip(self) -> QWidget:
        header_strip = QFrame()
        header_strip.setObjectName("actionStrip")

        header_layout = QHBoxLayout(header_strip)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.setSpacing(10)

        logo_label = QLabel()
        logo_path = resource_path(self.base_dir, "img", "brand.png")
        if logo_path.is_file():
            pixmap = QPixmap(str(logo_path))
            logo_label.setPixmap(pixmap.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            header_layout.addWidget(logo_label)

        title_column = QVBoxLayout()
        title_column.setSpacing(1)
        title_label = QLabel("Blender Render Farm Master")
        title_label.setStyleSheet("color: #eef4fb; font-size: 24px; font-weight: 700;")
        subtitle_label = QLabel(self._t("Distributed rendering control for Blender"))
        subtitle_label.setObjectName("captionLabel")
        title_column.addWidget(title_label)
        title_column.addWidget(subtitle_label)
        header_layout.addLayout(title_column)
        header_layout.addStretch(1)

        version_label = QLabel(f"v{self.runtime_metadata['appVersion']}")
        version_label.setObjectName("captionLabel")
        header_layout.addWidget(version_label)

        self.header_credit_label = QLabel(
            '<a href="https://novaxd.tech/" style="color:#76c4ff;text-decoration:none;">'
            "By Vinicius D'Arcadia Cruz - https://novaxd.tech/"
            "</a>"
        )
        self.header_credit_label.setTextFormat(Qt.RichText)
        self.header_credit_label.setOpenExternalLinks(True)
        self.header_credit_label.setToolTip(self._t("Open {url}", url="https://novaxd.tech/"))
        header_layout.addWidget(self.header_credit_label, 0, Qt.AlignRight | Qt.AlignVCenter)

        self.language_combo = QComboBox()
        self.language_combo.setFixedWidth(112)
        for code in ("en", "pt", "es"):
            self.language_combo.addItem(LANGUAGE_NAMES[code], code)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(self.language)))
        self.language_combo.currentIndexChanged.connect(self._on_language_selected)
        header_layout.addWidget(self.language_combo)
        return header_strip

    def _on_language_selected(self, _index: int) -> None:
        if not hasattr(self, "language_combo"):
            return
        language = normalize_language(self.language_combo.currentData())
        if language == self.language:
            return
        snapshot = None
        try:
            if self.mode == "coordinator" and hasattr(self.job_manager, "set_language"):
                snapshot = self.job_manager.set_language(language)
        except Exception as exc:
            QMessageBox.warning(self, self._t("Language"), translate_message(exc, self.language))
            return
        self._set_language(language, snapshot=snapshot, persist=True)

    def _set_language(
        self,
        language: str,
        *,
        snapshot: dict[str, Any] | None = None,
        persist: bool,
    ) -> None:
        normalized = normalize_language(language)
        if normalized == self.language:
            return
        form = self._capture_form_state()
        self.language = normalized
        if self.settings is not None:
            self.settings.language = normalized
            if persist:
                save_settings(self.persistence.config_path, self.settings)
        self._build_ui(int(form["block_size"]))
        self._restore_form_state(form)
        self._configure_mode_ui()
        active_snapshot = snapshot or self._last_snapshot
        if active_snapshot:
            self._apply_snapshot(active_snapshot)

    def _capture_form_state(self) -> dict[str, Any]:
        if not hasattr(self, "blend_edit"):
            return {"blend": "", "output": "", "start": 1, "end": 120, "block_size": 10,
                    "ignore": True, "redistribute": True, "auto_add": True}
        return {
            "blend": self.blend_edit.text(), "output": self.output_edit.text(),
            "start": self.start_frame_spin.value(), "end": self.end_frame_spin.value(),
            "block_size": self.block_size_spin.value(),
            "ignore": self.ignore_frames_check.isChecked(),
            "redistribute": self.redistribute_check.isChecked(),
            "auto_add": self.auto_add_check.isChecked(),
            "search": self.worker_search_edit.text(),
            "status_filter": self.worker_status_filter.currentData(),
        }

    def _restore_form_state(self, state: dict[str, Any]) -> None:
        self.blend_edit.setText(str(state["blend"]))
        self.output_edit.setText(str(state["output"]))
        self.start_frame_spin.setValue(int(state["start"]))
        self.end_frame_spin.setValue(int(state["end"]))
        self.block_size_spin.setValue(int(state["block_size"]))
        self.ignore_frames_check.setChecked(bool(state["ignore"]))
        self.redistribute_check.setChecked(bool(state["redistribute"]))
        self.auto_add_check.setChecked(bool(state["auto_add"]))
        self.worker_search_edit.setText(str(state.get("search", "")))
        self.worker_status_filter.setCurrentIndex(max(0, self.worker_status_filter.findData(state.get("status_filter", "all"))))

    def _build_ui(self, default_block_size: int) -> None:
        central = QWidget()
        central.setObjectName("centralRoot")
        self.content_scroll = scrollable(central, min_width=1080)
        self.setCentralWidget(self.content_scroll)

        main_layout = QVBoxLayout(central)
        main_layout.setSizeConstraint(QLayout.SetMinAndMaxSize)
        main_layout.setContentsMargins(8, 8, 8, 6)
        main_layout.setSpacing(6)
        main_layout.addWidget(self._build_header_strip())

        top_layout = QHBoxLayout()
        top_layout.setSpacing(8)

        project_group = QGroupBox(self._t("Project / Render"))
        project_layout = QGridLayout(project_group)
        project_layout.setContentsMargins(4, 2, 4, 2)
        project_layout.setHorizontalSpacing(10)
        project_layout.setVerticalSpacing(8)
        project_layout.setColumnStretch(1, 1)
        project_layout.setColumnStretch(3, 1)
        project_layout.setColumnStretch(5, 1)

        self.blend_edit = BlendFileLineEdit(language=self.language)
        self.blend_edit.fileDropped.connect(self._on_blend_dropped)
        self.blend_edit.textChanged.connect(self._on_blend_path_changed)
        self.select_blend_button = QPushButton(self._t("Select .blend"))
        self.select_blend_button.setFixedWidth(164)
        self.select_blend_button.clicked.connect(self._select_blend_file)
        self._set_button_icon(self.select_blend_button, "SP_DialogOpenButton")

        self.start_frame_spin = QSpinBox()
        self.start_frame_spin.setRange(0, 1000000)
        self.start_frame_spin.setValue(1)

        self.end_frame_spin = QSpinBox()
        self.end_frame_spin.setRange(0, 1000000)
        self.end_frame_spin.setValue(120)

        self.block_size_spin = QSpinBox()
        self.block_size_spin.setRange(1, 10000)
        self.block_size_spin.setValue(default_block_size)
        self.block_size_spin.setToolTip(
            self._t("Blocks organize the queue; individual frames are dispatched to balance machines with different speeds.")
        )

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(self._t("Select the frame output folder"))
        self.select_output_button = QPushButton(self._t("Select output folder"))
        self.select_output_button.setMinimumWidth(214)
        self.select_output_button.clicked.connect(self._select_output_folder)
        self._set_button_icon(self.select_output_button, "SP_DirOpenIcon")
        self.select_output_button.ensurePolished()
        self.select_output_button.setMinimumWidth(max(214, self.select_output_button.sizeHint().width()))

        project_layout.addWidget(QLabel(self._t("Blend file")), 0, 0)
        project_layout.addWidget(self.blend_edit, 0, 1, 1, 5)
        project_layout.addWidget(self.select_blend_button, 0, 6)
        project_layout.addWidget(QLabel(self._t("Start frame")), 1, 0)
        project_layout.addWidget(self.start_frame_spin, 1, 1)
        project_layout.addWidget(QLabel(self._t("End frame")), 1, 2)
        project_layout.addWidget(self.end_frame_spin, 1, 3)
        project_layout.addWidget(QLabel(self._t("Block size")), 1, 4)
        project_layout.addWidget(self.block_size_spin, 1, 5, 1, 2)
        project_layout.addWidget(QLabel(self._t("Output folder")), 2, 0)
        project_layout.addWidget(self.output_edit, 2, 1, 1, 5)
        project_layout.addWidget(self.select_output_button, 2, 6)

        self.detected_blend_version_label = QLabel("-")
        self.detected_blend_version_label.setObjectName("infoValue")
        self.detected_blend_policy_label = QLabel("-")
        self.detected_blend_policy_label.setObjectName("infoValue")

        info_strip = QFrame()
        info_strip.setObjectName("infoStrip")
        info_strip.setMinimumHeight(46)
        info_layout = QHBoxLayout(info_strip)
        info_layout.setContentsMargins(10, 8, 10, 8)
        info_layout.setSpacing(12)
        info_layout.addWidget(self._build_info_pair(self._t("File Blender"), self.detected_blend_version_label), 1)
        info_layout.addWidget(self._build_info_pair(self._t("Policy"), self.detected_blend_policy_label), 1)
        project_layout.addWidget(info_strip, 3, 0, 1, 7)

        self.ignore_frames_check = QCheckBox(self._t("Skip already rendered frames"))
        self.ignore_frames_check.setChecked(True)
        self.auto_add_check = QCheckBox(self._t("Automatically add new machines to the current job"))
        self.auto_add_check.setChecked(True)
        self.auto_add_check.stateChanged.connect(self._sync_live_job_flags)
        self.redistribute_check = QCheckBox(self._t("Redistribute blocks from offline machines"))
        self.redistribute_check.setChecked(True)
        self.redistribute_check.stateChanged.connect(self._sync_live_job_flags)

        policy_group = QGroupBox(self._t("Policy / Options"))
        policy_layout = QVBoxLayout(policy_group)
        policy_layout.setSpacing(10)
        policy_layout.addWidget(self.ignore_frames_check)
        policy_layout.addWidget(self.redistribute_check)
        policy_layout.addWidget(self.auto_add_check)
        policy_layout.addStretch(1)

        top_layout.addWidget(project_group, 3)
        top_layout.addWidget(policy_group, 2)
        main_layout.addLayout(top_layout)

        action_strip = QFrame()
        action_strip.setObjectName("actionStrip")
        action_layout = QVBoxLayout(action_strip)
        action_buttons = AdaptiveGrid(columns=8, compact_columns=4, breakpoint=1420)
        action_layout.addWidget(action_buttons)
        action_layout.setContentsMargins(10, 6, 10, 6)
        action_layout.setSpacing(8)

        self.start_button = QPushButton(self._t("Send to queue"))
        self.start_button.clicked.connect(self._start_render)
        self._set_button_variant(self.start_button, "primary")
        self._set_button_icon(self.start_button, "SP_MediaPlay")

        self.pause_button = QPushButton(self._t("Pause distribution"))
        self.pause_button.clicked.connect(self._pause_distribution)
        self._set_button_variant(self.pause_button, "info")
        self._set_button_icon(self.pause_button, "SP_MediaPause")

        self.resume_button = QPushButton(self._t("Resume distribution"))
        self.resume_button.clicked.connect(self._resume_distribution)
        self._set_button_variant(self.resume_button, "info")
        self._set_button_icon(self.resume_button, "SP_MediaPlay")

        self.cancel_button = QPushButton(self._t("Cancel job"))
        self.cancel_button.clicked.connect(self._cancel_job)
        self._set_button_variant(self.cancel_button, "danger")
        self._set_button_icon(self.cancel_button, "SP_BrowserStop")

        self.rescan_button = QPushButton(self._t("Rescan frames"))
        self.rescan_button.clicked.connect(self._rescan_frames)
        self._set_button_icon(self.rescan_button, "SP_BrowserReload")

        self.logs_button = QPushButton(self._t("Open logs"))
        self.logs_button.clicked.connect(self._show_logs_dialog)
        self._set_button_icon(self.logs_button, "SP_FileDialogDetailedView")

        self.save_button = QPushButton(self._t("Save job"))
        self.save_button.clicked.connect(self._save_job)
        self._set_button_icon(self.save_button, "SP_DialogSaveButton")

        self.load_button = QPushButton(self._t("Load job"))
        self.load_button.clicked.connect(self._load_job)
        self._set_button_icon(self.load_button, "SP_DialogOpenButton")

        for button in [
            self.start_button,
            self.pause_button,
            self.resume_button,
            self.cancel_button,
            self.rescan_button,
            self.logs_button,
            self.save_button,
            self.load_button,
        ]:
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            action_buttons.addWidget(button)
        main_layout.addWidget(action_strip)

        progress_group = QGroupBox(self._t("Overall progress"))
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setContentsMargins(2, 0, 2, 0)
        progress_layout.setSpacing(6)

        cards_layout = AdaptiveGrid(columns=10, compact_columns=5, breakpoint=1550)

        self.job_card = MetricCard(self._t("Job name"), "neutral", icon_path=resource_path(self.base_dir, "img", "icons", "file-text_blue_28.png"))
        self.total_frames_card = MetricCard(self._t("Total frames"), "neutral", icon_path=resource_path(self.base_dir, "img", "icons", "layers_blue_28.png"))
        self.rendering_frames_card = MetricCard(self._t("Rendering"), "blue", icon_path=resource_path(self.base_dir, "img", "icons", "play_blue_28.png"))
        self.completed_frames_card = MetricCard(self._t("Completed"), "green", icon_path=resource_path(self.base_dir, "img", "icons", "circle-check_blue_28.png"))
        self.error_frames_card = MetricCard(self._t("Errors"), "red", icon_path=resource_path(self.base_dir, "img", "icons", "circle-x_blue_28.png"))
        self.pending_frames_card = MetricCard(self._t("Pending"), "orange", icon_path=resource_path(self.base_dir, "img", "icons", "clock_blue_28.png"))
        self.online_machines_card = MetricCard(self._t("Machines online"), "green", icon_path=resource_path(self.base_dir, "img", "icons", "users_blue_28.png"))
        self.rendering_machines_card = MetricCard(self._t("Machines rendering"), "blue", icon_path=resource_path(self.base_dir, "img", "icons", "settings_blue_28.png"))
        self.eta_card = MetricCard("ETA", "neutral", icon_path=resource_path(self.base_dir, "img", "icons", "timer_blue_28.png"))
        self.elapsed_card = MetricCard(self._t("Elapsed time"), "neutral", icon_path=resource_path(self.base_dir, "img", "icons", "clock_blue_28.png"))

        for card in [
            self.job_card,
            self.total_frames_card,
            self.rendering_frames_card,
            self.completed_frames_card,
            self.error_frames_card,
            self.pending_frames_card,
            self.online_machines_card,
            self.rendering_machines_card,
            self.eta_card,
            self.elapsed_card,
        ]:
            card.setMinimumWidth(128)
            cards_layout.addWidget(card)
        progress_layout.addWidget(cards_layout)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        progress_label = QLabel(self._t("Overall progress"))
        progress_label.setObjectName("mutedLabel")
        self.general_progress_bar = QProgressBar()
        self.general_progress_bar.setRange(0, 100)
        self.general_progress_bar.setValue(0)
        self.general_progress_bar.setFormat("0.0%")
        self.general_progress_bar.setFixedHeight(22)
        self.progress_detail_label = QLabel("0 / 0 frames")
        self.progress_detail_label.setObjectName("mutedLabel")
        progress_row.addWidget(progress_label)
        progress_row.addWidget(self.general_progress_bar, 1)
        progress_row.addWidget(self.progress_detail_label)
        progress_layout.addLayout(progress_row)
        main_layout.addWidget(progress_group)

        worker_group = QGroupBox(self._t("Workers / Machines"))
        worker_layout = QVBoxLayout(worker_group)
        worker_layout.setContentsMargins(2, 0, 2, 0)
        worker_layout.setSpacing(6)

        worker_toolbar = QHBoxLayout()
        worker_toolbar.setSpacing(8)

        self.add_worker_button = QPushButton(self._t("Add worker"))
        self.add_worker_button.clicked.connect(self._add_worker)
        self._set_button_icon(self.add_worker_button, "SP_FileDialogNewFolder")

        self.refresh_workers_button = QPushButton(self._t("Refresh workers"))
        self.refresh_workers_button.clicked.connect(self._refresh_workers)
        self._set_button_icon(self.refresh_workers_button, "SP_BrowserReload")

        self.test_worker_button = QPushButton(self._t("Test selected"))
        self.test_worker_button.clicked.connect(self._test_selected_worker)
        self._set_button_icon(self.test_worker_button, "SP_DialogApplyButton")

        self.device_worker_button = QPushButton(self._t("Change device"))
        self.device_worker_button.clicked.connect(self._change_selected_worker_device)
        self._set_button_icon(self.device_worker_button, "SP_ComputerIcon")

        self.remove_worker_button = QPushButton(self._t("Remove selected"))
        self.remove_worker_button.clicked.connect(self._remove_selected_worker)
        self._set_button_variant(self.remove_worker_button, "danger")
        self._set_button_icon(self.remove_worker_button, "SP_TrashIcon")

        worker_toolbar.addWidget(self.add_worker_button)
        worker_toolbar.addWidget(self.refresh_workers_button)
        worker_toolbar.addWidget(self.test_worker_button)
        worker_toolbar.addWidget(self.device_worker_button)
        worker_toolbar.addWidget(self.remove_worker_button)
        worker_toolbar.addStretch(1)

        self.worker_search_edit = QLineEdit()
        self.worker_search_edit.setPlaceholderText(self._t("Search worker..."))
        self.worker_search_edit.setClearButtonEnabled(True)
        self.worker_search_edit.setMinimumWidth(130)
        self.worker_search_edit.setMaximumWidth(220)
        self.worker_search_edit.textChanged.connect(self._refresh_worker_table_view)

        self.worker_status_filter = QComboBox()
        self.worker_status_filter.setFixedWidth(160)
        self.worker_status_filter.addItem(self._t("All statuses"), "all")
        self.worker_status_filter.addItem(self._t("Rendering"), "rendering")
        self.worker_status_filter.addItem(self._t("Online"), "idle")
        self.worker_status_filter.addItem(self._t("Offline"), "offline")
        self.worker_status_filter.addItem(self._t("Error"), "error")
        self.worker_status_filter.addItem(self._t("Alert"), "alert")
        self.worker_status_filter.addItem(self._t("Timeout"), "timeout")
        self.worker_status_filter.addItem(self._t("Disabled"), "disabled")
        self.worker_status_filter.currentIndexChanged.connect(self._refresh_worker_table_view)

        worker_toolbar.addWidget(self.worker_search_edit)
        worker_toolbar.addWidget(self.worker_status_filter)
        worker_layout.addLayout(worker_toolbar)

        self.worker_job_tabs = QTabBar()
        self.worker_job_tabs.setDocumentMode(True)
        self.worker_job_tabs.setDrawBase(False)
        self.worker_job_tabs.setExpanding(False)
        self.worker_job_tabs.setMovable(False)
        self.worker_job_tabs.setUsesScrollButtons(True)
        self.worker_job_tabs.currentChanged.connect(self._on_worker_job_tab_changed)
        worker_layout.addWidget(self.worker_job_tabs)

        self.workers_table = QTableWidget(0, 15)
        self.workers_table.setHorizontalHeaderLabels(
            [
                self._t("Use"),
                self._t("Name"),
                "IP",
                self._t("Port"),
                self._t("Status"),
                "Blender",
                self._t("Version"),
                "GPU",
                self._t("Render device"),
                self._t("Effective GPU"),
                self._t("Blend access"),
                self._t("Output access"),
                self._t("Progress"),
                self._t("Current frame"),
                self._t("Last heartbeat"),
            ]
        )
        self.workers_table.setFixedHeight(150)
        self.workers_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.workers_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.workers_table.verticalHeader().setVisible(False)
        self.workers_table.verticalHeader().setDefaultSectionSize(30)
        self.workers_table.setAlternatingRowColors(True)
        worker_header = self.workers_table.horizontalHeader()
        worker_header.setSectionResizeMode(QHeaderView.ResizeToContents)
        worker_header.setSectionResizeMode(7, QHeaderView.Stretch)
        worker_header.setSectionResizeMode(12, QHeaderView.Fixed)
        self.workers_table.setColumnWidth(12, 170)
        self.workers_table.itemSelectionChanged.connect(self._update_worker_log_panel)
        worker_layout.addWidget(self.workers_table, 1)
        main_layout.addWidget(worker_group, 1)

        self.queue_group = QGroupBox(self._t("Global queue"))
        queue_layout = QVBoxLayout(self.queue_group)
        queue_layout.setContentsMargins(2, 0, 2, 0)
        queue_layout.setSpacing(6)

        self.queue_table = QTableWidget(0, 7)
        self.queue_table.setHorizontalHeaderLabels(
            [
                self._t("Job"),
                self._t("Status"),
                "Master",
                self._t("User"),
                self._t("Created at"),
                self._t("Position"),
                self._t("frames"),
            ]
        )
        self.queue_table.setFixedHeight(114)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.queue_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.verticalHeader().setDefaultSectionSize(28)
        self.queue_table.setAlternatingRowColors(True)
        queue_header = self.queue_table.horizontalHeader()
        queue_header.setSectionResizeMode(QHeaderView.ResizeToContents)
        queue_header.setSectionResizeMode(0, QHeaderView.Stretch)
        queue_header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.queue_table.itemSelectionChanged.connect(self._on_queue_selection_changed)
        queue_layout.addWidget(self.queue_table, 1)

        self.queue_group.setMaximumHeight(190)
        main_layout.addWidget(self.queue_group)

        footer_bar = QFrame()
        footer_bar.setObjectName("footerBar")
        footer_layout = QVBoxLayout(footer_bar)
        footer_items = AdaptiveGrid(columns=5, compact_columns=3, breakpoint=1440)
        footer_layout.addWidget(footer_items)
        footer_layout.setContentsMargins(10, 6, 10, 6)
        footer_layout.setSpacing(10)

        self.connection_indicator = QFrame()
        self.connection_indicator.setFixedSize(10, 10)
        self.connection_indicator.setStyleSheet("background: #7d8aa0; border-radius: 5px;")
        self.connection_status_label = QLabel(self._t("Waiting for connection"))
        self.connection_status_label.setObjectName("footerText")
        self.footer_workers_label = QLabel(f"{self._t('Workers online')}: 0 / 0")
        self.footer_workers_label.setObjectName("footerText")
        self.footer_rendering_label = QLabel(f"{self._t('Machines rendering')}: 0")
        self.footer_rendering_label.setObjectName("footerText")
        self.footer_version_label = QLabel(f"Master v{self.runtime_metadata['appVersion']}")
        self.footer_version_label.setObjectName("footerText")
        self.local_time_label = QLabel(self._t("Local time: {value}", value="--"))
        self.local_time_label.setObjectName("footerText")

        self.settings_button = QPushButton(self._t("Settings"))
        self.settings_button.clicked.connect(self._show_runtime_details)
        self._set_button_variant(self.settings_button, "footer")
        self._set_button_icon(self.settings_button, "SP_FileDialogDetailedView")

        connection_box = QWidget()
        connection_layout = QHBoxLayout(connection_box)
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_layout.addWidget(self.connection_indicator)
        self.connection_status_label.setWordWrap(True)
        connection_layout.addWidget(self.connection_status_label, 1)
        self.local_time_label.setWordWrap(True)
        footer_items.addWidget(connection_box)
        footer_items.addWidget(self.footer_workers_label)
        footer_items.addWidget(self.footer_rendering_label)
        footer_items.addWidget(self.local_time_label)
        footer_items.addWidget(self.settings_button)
        main_layout.addWidget(footer_bar)

    def _load_initial_snapshot(self) -> None:
        snapshot = self.job_manager.get_snapshot()
        self._apply_snapshot(snapshot)

    def _on_poll_timer(self) -> None:
        if self._tick_future and self._tick_future.done():
            try:
                snapshot = self._tick_future.result()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Erro no ciclo de polling", exc)
                QMessageBox.warning(self, self._t("Polling failed"), translate_message(exc, self.language))
            else:
                self._apply_snapshot(snapshot)
            finally:
                self._tick_future = None

        if self._discovery_future and self._discovery_future.done():
            try:
                result = self._discovery_future.result()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Erro ao descobrir workers", exc)
                QMessageBox.warning(self, self._t("Could not refresh workers"), translate_message(exc, self.language))
            else:
                snapshot = result.get("snapshot", self.job_manager.get_snapshot())
                self._apply_snapshot(snapshot)
                self._show_text_report(self._t("Refresh workers"), self._format_discovery_report(result))
            finally:
                self._discovery_future = None
                self.refresh_workers_button.setEnabled(True)
                self.refresh_workers_button.setText(self._t("Refresh workers"))

        if self._tick_future is None:
            self._tick_future = self._executor.submit(self.job_manager.tick)

    def _apply_snapshot(self, snapshot: dict) -> None:
        coordinator = snapshot.get("coordinator") or {}
        coordinator_language = coordinator.get("language")
        if coordinator_language and normalize_language(coordinator_language) != self.language:
            self._set_language(str(coordinator_language), snapshot=snapshot, persist=True)
            return
        self._last_snapshot = snapshot
        workers = snapshot.get("workers", [])
        self._all_workers = workers
        metrics = snapshot.get("metrics", {})
        job = snapshot.get("job")
        jobs = self._snapshot_jobs_for_worker_views(snapshot, job)
        queue = snapshot.get("queue", [])

        if job:
            self.auto_add_check.blockSignals(True)
            self.redistribute_check.blockSignals(True)
            self.auto_add_check.setChecked(bool(job.get("autoAddWorkers", True)))
            self.redistribute_check.setChecked(bool(job.get("redistributeOfflineBlocks", True)))
            self.auto_add_check.blockSignals(False)
            self.redistribute_check.blockSignals(False)

        self._refresh_blend_version_labels(job)
        self._rebuild_queue_table(queue, job, jobs)
        self._rebuild_worker_job_tabs(workers, jobs, job)
        self._refresh_progress_overview(metrics=metrics, primary_job=job, jobs=jobs, workers=workers)
        self._rebuild_worker_table(workers, job, jobs)

        logs = snapshot.get("logs", {})
        self._master_log_source = str(logs.get("general") or self.logger.general_text())
        self._update_worker_log_panel()
        self._update_footer(snapshot=snapshot, metrics=metrics, workers=workers)
        self._update_buttons(job)

    def _rebuild_queue_table(
        self,
        queue: list[dict],
        active_job: dict | None = None,
        jobs: list[dict] | None = None,
    ) -> None:
        items = self._visible_queue_jobs(queue=queue, active_job=active_job, jobs=jobs or [])

        previous_selection = self.selected_queue_job_id
        self.queue_table.blockSignals(True)
        self.queue_table.clearSelection()
        self.queue_table.setRowCount(len(items))

        for row, item in enumerate(items):
            job_id = str(item.get("jobId") or item.get("id") or "")
            job_name = str(item.get("jobName") or job_id or "-")
            name_item = QTableWidgetItem(job_name)
            name_item.setData(Qt.UserRole, job_id)
            self.queue_table.setItem(row, 0, name_item)

            status_text, status_color = job_status_view(item, self.language)
            self.queue_table.setCellWidget(row, 1, create_status_label(status_text, status_color))

            self.queue_table.setItem(row, 2, QTableWidgetItem(str(item.get("submittedByMasterName") or "-")))
            self.queue_table.setItem(row, 3, QTableWidgetItem(str(item.get("submittedByUser") or "-")))
            self.queue_table.setItem(row, 4, QTableWidgetItem(str(item.get("createdAt") or "-")))

            position = item.get("queuePosition")
            if position in {None, ""} and active_job and item.get("jobId") == active_job.get("jobId"):
                position = 0
            position_item = QTableWidgetItem(str(position if position is not None else "-"))
            position_item.setTextAlignment(Qt.AlignCenter)
            self.queue_table.setItem(row, 5, position_item)

            frame_total = item.get("totalFrames")
            if frame_total in {None, ""}:
                start_frame = item.get("startFrame")
                end_frame = item.get("endFrame")
                if start_frame is not None and end_frame is not None:
                    frame_total = int(end_frame) - int(start_frame) + 1
            frames_item = QTableWidgetItem(str(frame_total if frame_total not in {None, ""} else "-"))
            frames_item.setTextAlignment(Qt.AlignCenter)
            self.queue_table.setItem(row, 6, frames_item)

            if previous_selection and previous_selection == job_id:
                self.queue_table.selectRow(row)

        self.queue_table.blockSignals(False)
        self.queue_group.setTitle(self._t("Global queue") + " · " + self._t("{count} jobs in queue", count=len(items)))
        self.queue_group.setVisible(self.mode == "coordinator")

    def _rebuild_worker_table(
        self,
        workers: list[dict],
        job: dict | None = None,
        jobs: list[dict] | None = None,
    ) -> None:
        previous_selection = self.selected_worker_id
        selected_job_id = self._selected_worker_job_tab_id()
        visible_workers = [
            worker
            for worker in workers
            if self._matches_worker_filters(worker) and self._matches_worker_job_tab(worker, selected_job_id)
        ]
        job_lookup = self._job_lookup_for_worker_views(jobs or [], job)
        blocks_by_job_id = {
            job_id: {
                block.get("blockId"): block
                for block in (job_payload.get("blocks", []) if isinstance(job_payload, dict) else [])
                if isinstance(block, dict) and block.get("blockId")
            }
            for job_id, job_payload in job_lookup.items()
        }
        self._visible_worker_ids = {
            str(worker.get("id") or "")
            for worker in visible_workers
            if str(worker.get("id") or "").strip()
        }
        restored_selection = False

        self.workers_table.blockSignals(True)
        self.workers_table.clearSelection()
        self.workers_table.setRowCount(len(visible_workers))

        for row, worker in enumerate(visible_workers):
            worker_id = str(worker["id"])
            select_checkbox = QCheckBox()
            select_checkbox.setChecked(bool(worker.get("selected", True)))
            if getattr(self.job_manager, "supports_worker_selection", True):
                select_checkbox.stateChanged.connect(
                    lambda state, wid=worker_id: self.job_manager.update_worker_selected(wid, state == Qt.Checked)
                )
            else:
                select_checkbox.setEnabled(False)
            self.workers_table.setCellWidget(row, 0, self._wrap_centered(select_checkbox))

            name_item = QTableWidgetItem(worker.get("name", ""))
            name_item.setData(Qt.UserRole, worker_id)
            self.workers_table.setItem(row, 1, name_item)

            ip_item = QTableWidgetItem(worker.get("ip", ""))
            port_item = QTableWidgetItem(str(worker.get("port", "")))
            port_item.setTextAlignment(Qt.AlignCenter)
            self.workers_table.setItem(row, 2, ip_item)
            self.workers_table.setItem(row, 3, port_item)

            status_text, status_color = worker_status_view(worker)
            self.workers_table.setCellWidget(row, 4, create_status_label(self._t(status_text), status_color))

            blender_found = self._centered_item(self._t("Yes") if worker.get("blenderFound") else self._t("No"))
            version_item = self._centered_item(worker.get("blenderVersion") or "-")
            self.workers_table.setItem(row, 5, blender_found)
            self.workers_table.setItem(row, 6, version_item)
            self.workers_table.setItem(row, 7, QTableWidgetItem(worker.get("gpu") or "-"))
            self.workers_table.setItem(row, 8, self._centered_item(worker.get("renderDevicePreference") or "-"))
            self.workers_table.setItem(row, 9, self._centered_item(self._format_effective_gpu(worker.get("effectiveUseGpu"))))
            self.workers_table.setItem(row, 10, self._build_access_item(worker.get("blendAccessible")))
            self.workers_table.setItem(row, 11, self._build_access_item(worker.get("outputAccessible")))

            worker_job = self._resolve_worker_job(worker, job_lookup, selected_job_id, job)
            worker_job_id = str(worker_job.get("jobId") or "") if isinstance(worker_job, dict) else ""
            blocks_by_id = blocks_by_job_id.get(worker_job_id, {})
            current_block = self._resolve_current_block(worker, blocks_by_id)
            progress_bar = QProgressBar()
            progress_bar.setRange(0, 100)
            progress_value = self._resolve_worker_progress_value(worker, current_block)
            progress_bar.setValue(int(max(0.0, min(100.0, progress_value))))
            progress_bar.setFormat(f"{progress_value:.1f}%")
            progress_bar.setFixedHeight(18)
            self.workers_table.setCellWidget(row, 12, progress_bar)

            frame_item = QTableWidgetItem(self._format_worker_frame_text(worker, worker_job, current_block))
            frame_item.setTextAlignment(Qt.AlignCenter)
            self.workers_table.setItem(row, 13, frame_item)

            heartbeat_item = QTableWidgetItem(self._format_relative_age(worker.get("lastSeen")))
            heartbeat_item.setTextAlignment(Qt.AlignCenter)
            self.workers_table.setItem(row, 14, heartbeat_item)

            if previous_selection and previous_selection == worker_id:
                self.workers_table.selectRow(row)
                restored_selection = True

        self.workers_table.blockSignals(False)
        if not restored_selection:
            self.selected_worker_id = None
        self._update_worker_action_buttons()

    def _update_worker_log_panel(self) -> None:
        current_row = self.workers_table.currentRow()
        if current_row >= 0:
            name_item = self.workers_table.item(current_row, 1)
            if name_item:
                self.selected_worker_id = str(name_item.data(Qt.UserRole) or "")
        elif self.selected_worker_id and (
            self.selected_worker_id not in self._visible_worker_ids or self._current_worker_payload() is None
        ):
            self.selected_worker_id = None

        self._update_worker_action_buttons()

    def _on_queue_selection_changed(self) -> None:
        current_row = self.queue_table.currentRow()
        if current_row < 0:
            self.selected_queue_job_id = None
            self._update_buttons(self._last_snapshot.get("job"))
            return

        item = self.queue_table.item(current_row, 0)
        if item:
            self.selected_queue_job_id = item.data(Qt.UserRole)
            self._update_buttons(self._last_snapshot.get("job"))

    def _update_buttons(self, job: dict | None) -> None:
        target_job = self._selected_queue_job_snapshot() if self.mode == "coordinator" else job
        status = target_job.get("status") if target_job else None
        self.pause_button.setEnabled(status == "running")
        self.resume_button.setEnabled(status == "paused")
        self.cancel_button.setEnabled(status in {"running", "paused", "error", "queued", "blocked"})
        self.rescan_button.setEnabled(job is not None and getattr(self.job_manager, "supports_rescan", True))
        self.save_button.setEnabled(job is not None and getattr(self.job_manager, "supports_local_jobs", True))
        self.load_button.setEnabled(getattr(self.job_manager, "supports_local_job_files", True))
        self._update_worker_action_buttons()

    def _update_worker_action_buttons(self) -> None:
        has_worker = self._current_worker_payload() is not None
        self.test_worker_button.setEnabled(has_worker)
        self.remove_worker_button.setEnabled(has_worker)
        self.device_worker_button.setEnabled(has_worker and getattr(self.job_manager, "supports_worker_device_update", True))

    def _select_blend_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self._t("Select .blend"), str(self.base_dir), "Blender (*.blend)")
        if path:
            self.blend_edit.setText(path)

    def _select_output_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, self._t("Select output folder"), str(self.base_dir))
        if path:
            self.output_edit.setText(path)

    def _on_blend_dropped(self, path: str) -> None:
        if not self.output_edit.text().strip():
            default_output = Path(path).with_suffix("")
            self.output_edit.setText(str(default_output.parent / f"{default_output.name}_renders"))

    def _on_blend_path_changed(self, path: str) -> None:
        self._local_blend_version = None
        self._local_blend_version_status = None
        normalized_path = path.strip()
        if normalized_path.lower().endswith(".blend"):
            blend_path = Path(normalized_path)
            if blend_path.exists():
                self._local_blend_version = read_blend_version_metadata(blend_path)
                if self._local_blend_version is None:
                    self._local_blend_version_status = "Not detected"
        self._refresh_blend_version_labels(self._last_snapshot.get("job"))

    def _refresh_blend_version_labels(self, job: dict | None = None) -> None:
        metadata = self._local_blend_version
        if metadata is None:
            metadata = blend_version_metadata_from_job(job)

        if metadata is not None:
            self.detected_blend_version_label.setText(metadata.required_blender_version)
            self.detected_blend_policy_label.setText(self._t("Prefer {major}.x >= {version}", major=metadata.preferred_blender_major, version=metadata.required_blender_version))
            return

        if self._local_blend_version_status:
            self.detected_blend_version_label.setText(self._t(self._local_blend_version_status))
            self.detected_blend_policy_label.setText("-")
            return

        self.detected_blend_version_label.setText("-")
        self.detected_blend_policy_label.setText("-")

    def _start_render(self) -> None:
        if not self.blend_edit.text().strip() or not self.output_edit.text().strip():
            QMessageBox.warning(self, self._t("Cannot submit job"),
                                self._t("Select a .blend file and an output folder before submitting."))
            return
        try:
            proceed, version_routing_policy_override = self._decide_version_routing_override()
            if not proceed:
                return
            warnings = self.job_manager.start_job(
                blend_file=self.blend_edit.text().strip(),
                start_frame=self.start_frame_spin.value(),
                end_frame=self.end_frame_spin.value(),
                output_folder=self.output_edit.text().strip(),
                block_size=self.block_size_spin.value(),
                ignore_rendered_frames=self.ignore_frames_check.isChecked(),
                auto_add_workers=self.auto_add_check.isChecked(),
                redistribute_offline_blocks=self.redistribute_check.isChecked(),
                version_routing_policy_override=version_routing_policy_override,
            )
        except WorkerPathValidationError as exc:
            self._show_text_report(self._t("Path validation failed"), translate_message(exc.detailed_text(), self.language))
            return
        except (JobValidationError, Exception) as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Cannot submit job"), translate_message(exc, self.language))
            return

        self._apply_snapshot(self.job_manager.get_snapshot())
        if warnings:
            title = self._t("Global queue updated") if self.mode == "coordinator" else self._t("Job started with warnings")
            self._show_text_report(title, "\n".join(warnings))

    def _decide_version_routing_override(self) -> tuple[bool, str | None]:
        preflight_callable = getattr(self.job_manager, "preflight_job", None)
        if not callable(preflight_callable):
            return True, None

        preflight = preflight_callable(
            blend_file=self.blend_edit.text().strip(),
            start_frame=self.start_frame_spin.value(),
            end_frame=self.end_frame_spin.value(),
            output_folder=self.output_edit.text().strip(),
            block_size=self.block_size_spin.value(),
            ignore_rendered_frames=self.ignore_frames_check.isChecked(),
            auto_add_workers=self.auto_add_check.isChecked(),
            redistribute_offline_blocks=self.redistribute_check.isChecked(),
        )
        if not isinstance(preflight, dict) or not preflight.get("needsVersionChoice"):
            return True, None

        return self._show_version_routing_dialog(preflight)

    def _show_version_routing_dialog(self, preflight: dict[str, Any]) -> tuple[bool, str | None]:
        job_info = preflight.get("job") or {}
        version = str(job_info.get("requiredBlenderVersion") or job_info.get("blendVersionDetected") or self._t("Unknown"))
        older = preflight.get("sameMajorOlderVersions") or [preflight.get("bestSameMajorOlderVersion")]
        higher = preflight.get("higherMajorVersions") or [preflight.get("bestHigherMajorVersion")]
        older = [str(v) for v in older if v]
        higher = [str(v) for v in higher if v]
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle(self._t("Blender version choice"))
        dialog.setText(self._t("This file requires Blender {version}. Available workers with the same major version are older.", version=version))
        lines = [self._t("Older versions in the same major series") + ": " + (", ".join(older) or "-"),
                 self._t("Higher available versions") + ": " + (", ".join(higher) or self._t("No higher major version is available.")),
                 self._t("Select which available Blender version should render this file.")]
        dialog.setInformativeText("\n".join(lines))
        older_button = dialog.addButton(self._t("Use older version"), QMessageBox.AcceptRole)
        higher_button = dialog.addButton(self._t("Use higher version"), QMessageBox.ActionRole)
        higher_button.setEnabled(bool(higher))
        dialog.addButton(self._t("Cancel"), QMessageBox.RejectRole)
        dialog.exec()
        if dialog.clickedButton() is older_button:
            return True, PREFER_SAME_MAJOR_ANY_VERSION_POLICY
        if dialog.clickedButton() is higher_button and higher:
            return True, PREFER_HIGHER_MAJOR_WHEN_SAME_MAJOR_OLDER_POLICY
        return False, None

    def _pause_distribution(self) -> None:
        try:
            target_job_id = self._selected_job_id_for_action()
            self.job_manager.pause_distribution(target_job_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Could not pause"), translate_message(exc, self.language))
            return
        self._apply_snapshot(self.job_manager.get_snapshot())

    def _resume_distribution(self) -> None:
        try:
            target_job_id = self._selected_job_id_for_action()
            self.job_manager.resume_distribution(target_job_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Could not resume"), translate_message(exc, self.language))
            return
        self._apply_snapshot(self.job_manager.get_snapshot())

    def _cancel_job(self) -> None:
        if not self._confirm("Cancel job", "Cancel the selected job?"):
            return
        try:
            target_job_id = self._selected_job_id_for_action()
            self.job_manager.cancel_job(target_job_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Could not cancel"), translate_message(exc, self.language))
            return
        self._apply_snapshot(self.job_manager.get_snapshot())

    def _rescan_frames(self) -> None:
        if not getattr(self.job_manager, "supports_rescan", True):
            QMessageBox.information(
                self,
                self._t("Central rescan"),
                self._t("The Coordinator manages frame rescans."),
            )
            return
        try:
            self.job_manager.force_rescan()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Could not rescan"), translate_message(exc, self.language))
            return
        self._apply_snapshot(self.job_manager.get_snapshot())

    def _save_job(self) -> None:
        if not getattr(self.job_manager, "supports_local_jobs", True):
            QMessageBox.information(
                self,
                self._t("Central queue"),
                self._t("The Coordinator persists the shared queue automatically."),
            )
            return
        try:
            path = self.job_manager.save_current_job()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Could not save"), translate_message(exc, self.language))
            return
        QMessageBox.information(self, self._t("Job saved"), self._t("State saved to:\n{path}", path=path))

    def _load_job(self) -> None:
        if not getattr(self.job_manager, "supports_local_job_files", True):
            QMessageBox.information(
                self,
                self._t("Central queue"),
                self._t("Local job loading is unavailable in Coordinator mode."),
            )
            return

        path, _ = QFileDialog.getOpenFileName(
            self,
            self._t("Load job"),
            str(self.persistence.jobs_dir),
            "JSON (*.json)",
        )
        if not path:
            return

        try:
            self.job_manager.load_job(Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Could not load job"), translate_message(exc, self.language))
            return

        snapshot = self.job_manager.get_snapshot()
        job = snapshot.get("job")
        if job:
            self.blend_edit.setText(job.get("blendFile", ""))
            self.start_frame_spin.setValue(int(job.get("startFrame", 1)))
            self.end_frame_spin.setValue(int(job.get("endFrame", 1)))
            self.output_edit.setText(job.get("outputFolder", ""))
            self.block_size_spin.setValue(int(job.get("blockSize", self.block_size_spin.value())))
            self.ignore_frames_check.setChecked(bool(job.get("ignoreRenderedFrames", True)))
        self._apply_snapshot(snapshot)

    def _add_worker(self) -> None:
        dialog = WorkerDialog(default_port=self.default_worker_port, parent=self, language=self.language)
        if dialog.exec() != QDialog.Accepted:
            return

        values = dialog.values()
        if not values["ip"]:
            QMessageBox.warning(self, self._t("Invalid worker"), self._t("Enter an IP address or hostname for the worker."))
            return

        try:
            self.job_manager.add_worker(
                name=str(values["name"]),
                ip=str(values["ip"]),
                port=int(values["port"]),
                enabled=bool(values["enabled"]),
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Could not add worker"), translate_message(exc, self.language))
            return
        self._apply_snapshot(self.job_manager.get_snapshot())

    def _refresh_workers(self) -> None:
        if self._discovery_future is not None:
            return
        self.refresh_workers_button.setEnabled(False)
        self.refresh_workers_button.setText(self._t("Searching..."))
        self._discovery_future = self._discovery_executor.submit(self.job_manager.discover_workers)

    def _remove_selected_worker(self) -> None:
        worker = self._current_worker_payload()
        if worker is None:
            QMessageBox.information(self, self._t("No worker selected"), self._t("Select a worker to remove."))
            return
        self._remove_worker(str(worker.get("id")))

    def _remove_worker(self, worker_id: str) -> None:
        if not self._confirm("Remove worker", "Remove this worker?"):
            return
        try:
            self.job_manager.remove_worker(worker_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Could not remove worker"), translate_message(exc, self.language))
            return
        if self.selected_worker_id == worker_id:
            self.selected_worker_id = None
        self._apply_snapshot(self.job_manager.get_snapshot())

    def _test_selected_worker(self) -> None:
        worker = self._current_worker_payload()
        if worker is None:
            QMessageBox.information(self, self._t("No worker selected"), self._t("Select a worker to test."))
            return
        self._test_worker(str(worker.get("id")))

    def _test_worker(self, worker_id: str) -> None:
        try:
            result = self.job_manager.test_worker(
                worker_id=worker_id,
                blend_file=self.blend_edit.text().strip() or None,
                output_folder=self.output_edit.text().strip() or None,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Test failed"), translate_message(exc, self.language))
            return

        status = result.get("status", {})
        validation = result.get("validate", {})
        render_device_preference = (
            status.get("renderDevicePreference")
            or result.get("capabilities", {}).get("renderDevicePreference")
            or result.get("renderDevice", {}).get("renderDevicePreference")
        )
        effective_use_gpu = status.get("effectiveUseGpu")
        if effective_use_gpu is None:
            effective_use_gpu = result.get("capabilities", {}).get("effectiveUseGpu")
        if effective_use_gpu is None:
            effective_use_gpu = result.get("renderDevice", {}).get("effectiveUseGpu")

        lines = [
            f"{self._t('Machine name')}: {status.get('machineName') or status.get('ip') or '-'}",
            f"{self._t('Status')}: {status.get('status', '-')}",
            f"{self._t('Blender found')}: {self._t('Yes') if status.get('blenderFound') else self._t('No')}",
        ]
        if render_device_preference:
            lines.append(f"{self._t('Render device')}: {render_device_preference}")
        if effective_use_gpu is not None:
            lines.append(f"{self._t('Effective GPU')}: {self._t('Yes') if effective_use_gpu else self._t('No')}")
        if validation:
            blend_exists = validation.get("blendFileExists", validation.get("blendAccessible"))
            output_exists = validation.get("outputFolderExists", validation.get("outputAccessible"))
            can_write = validation.get("canWriteOutput", validation.get("outputAccessible"))
            lines.append(f"{self._t('Blend file exists')}: {self._format_validation_value(blend_exists)}")
            lines.append(f"{self._t('Output folder exists')}: {self._format_validation_value(output_exists)}")
            lines.append(f"{self._t('Output is writable')}: {self._format_validation_value(can_write)}")
            errors = validation.get("errors")
            if isinstance(errors, list) and errors:
                lines.append(self._t("Errors") + ":")
                lines.extend([f"- {item}" for item in errors])
            if validation.get("message"):
                lines.append(f"{self._t('Worker message')}: {validation.get('message')}")

        QMessageBox.information(self, self._t("Test completed"), "\n".join(lines))
        self._apply_snapshot(self.job_manager.get_snapshot())

    def _change_selected_worker_device(self) -> None:
        worker = self._current_worker_payload()
        if worker is None:
            QMessageBox.information(self, self._t("No worker selected"), self._t("Select a worker to change its device."))
            return
        self._change_worker_device(str(worker.get("id")))

    def _change_worker_device(self, worker_id: str) -> None:
        if not getattr(self.job_manager, "supports_worker_device_update", True):
            QMessageBox.information(
                self,
                self._t("Action unavailable"),
                self._t("Device changes are unavailable through this Coordinator."),
            )
            return

        snapshot = self.job_manager.get_snapshot()
        worker = next((item for item in snapshot.get("workers", []) if item.get("id") == worker_id), None)
        if not worker:
            QMessageBox.warning(self, self._t("Worker not found"), self._t("The worker could not be found."))
            return

        dialog = QMessageBox(self)
        dialog.setWindowTitle(self._t("Change device"))
        dialog.setText(self._t("Select the render device for {name}", name=worker.get("name", "Worker")))
        cpu_button = dialog.addButton("CPU", QMessageBox.AcceptRole)
        gpu_button = dialog.addButton("GPU", QMessageBox.AcceptRole)
        dialog.addButton(self._t("Cancel"), QMessageBox.RejectRole)
        dialog.exec()

        clicked_button = dialog.clickedButton()
        if clicked_button not in {cpu_button, gpu_button}:
            return

        target_preference = "CPU" if clicked_button is cpu_button else "GPU"
        try:
            self.job_manager.update_worker_render_device(worker_id, target_preference)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self._t("Could not change device"), translate_message(exc, self.language))
            return

        self._apply_snapshot(self.job_manager.get_snapshot())
        QMessageBox.information(
            self,
            self._t("Render device updated"),
            self._t("{name} now prefers {device}.", name=worker.get("name", "Worker"), device=target_preference),
        )

    def _sync_live_job_flags(self) -> None:
        self.job_manager.update_job_options(
            auto_add_workers=self.auto_add_check.isChecked(),
            redistribute_offline_blocks=self.redistribute_check.isChecked(),
        )

    def _configure_mode_ui(self) -> None:
        if self.mode == "coordinator":
            self.setWindowTitle("Blender Render Farm Master")
            self.start_button.setText(self._t("Send to queue"))
            self.save_button.setToolTip(self._t("The Coordinator persists the shared queue automatically."))
            self.load_button.setToolTip(self._t("Local job loading is unavailable in Coordinator mode."))
            self.rescan_button.setToolTip(self._t("The Coordinator manages frame rescans."))
        else:
            self.queue_group.setVisible(False)

        if not getattr(self.job_manager, "supports_worker_device_update", True):
            self.device_worker_button.setToolTip(self._t("Device changes are unavailable through this Coordinator."))

    def _selected_job_id_for_action(self) -> str | None:
        if self.mode != "coordinator":
            return None
        return self.selected_queue_job_id

    def _selected_queue_job_snapshot(self) -> dict | None:
        if self.mode != "coordinator":
            return None
        if not self.selected_queue_job_id:
            return self._last_snapshot.get("job")

        active_job = self._last_snapshot.get("job")
        if active_job and active_job.get("jobId") == self.selected_queue_job_id:
            return active_job
        for item in self._last_snapshot.get("queue", []):
            if item.get("jobId") == self.selected_queue_job_id:
                return item
        for item in self._last_snapshot.get("jobs", []):
            if item.get("jobId") == self.selected_queue_job_id:
                return item
        return self._last_snapshot.get("job")

    def _worker_log_text(self, worker_id: str) -> str:
        logs = self._last_snapshot.get("logs", {})
        worker_logs = logs.get("workers", {})
        if isinstance(worker_logs, dict) and worker_id in worker_logs:
            value = worker_logs.get(worker_id)
            if isinstance(value, list):
                return "\n".join(str(item) for item in value)
            return str(value or "")

        for worker in self._last_snapshot.get("workers", []):
            if worker.get("id") == worker_id:
                return str(worker.get("lastLog") or "")
        return self.logger.worker_text(worker_id)

    def _format_access(self, value) -> str:
        if value is True:
            return self._t("Yes")
        if value is False:
            return self._t("No")
        return "-"

    def _format_effective_gpu(self, value) -> str:
        if value is True:
            return self._t("Yes")
        if value is False:
            return self._t("No")
        return "-"

    def _format_validation_value(self, value) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        return "unknown"

    def _show_text_report(self, title: str, body: str) -> None:
        dialog = TextReportDialog(title=title, body=translate_message(body, self.language), parent=self)
        dialog.exec()

    def _confirm(self, title: str, message: str) -> bool:
        dialog = QMessageBox(self)
        dialog.setWindowTitle(self._t(title))
        dialog.setText(self._t(message))
        dialog.setIcon(QMessageBox.Question)
        accept = dialog.addButton(self._t("Yes"), QMessageBox.YesRole)
        reject = dialog.addButton(self._t("No"), QMessageBox.NoRole)
        dialog.setDefaultButton(reject)
        dialog.exec()
        return dialog.clickedButton() is accept

    def _format_discovery_report(self, result: dict) -> str:
        lines = [
            self._t("Worker refresh completed."),
            "",
            f"{self._t('Workers found')}: {result.get('discoveredCount', 0)}",
            f"{self._t('New registrations')}: {result.get('addedCount', 0)}",
            f"{self._t('Updated')}: {result.get('updatedCount', 0)}",
            f"{self._t('Endpoints checked')}: {result.get('scannedCount', 0)}",
        ]
        scanned_networks = result.get("scannedNetworks") or []
        ports = result.get("ports") or []
        if scanned_networks:
            lines.append(f"{self._t('Networks checked')}: {', '.join(scanned_networks)}")
        if ports:
            lines.append(f"{self._t('Ports checked')}: {', '.join(str(item) for item in ports)}")

        workers = result.get("workers") or []
        if workers:
            lines.extend(["", self._t("Workers found")])
            lines.extend(
                [
                    f"- {item.get('name', 'Worker')} ({item.get('endpoint', '-')})"
                    for item in workers
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    self._t("No worker responded to this scan."),
                    self._t("Registered workers remain saved for later use."),
                ]
            )
        return "\n".join(lines)

    def _format_duration(self, seconds) -> str:
        if seconds is None:
            return "-"
        total_seconds = int(max(0, float(seconds)))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _refresh_worker_table_view(self, *_args) -> None:
        self._rebuild_worker_table(
            self._all_workers,
            self._last_snapshot.get("job"),
            self._snapshot_jobs_for_worker_views(self._last_snapshot, self._last_snapshot.get("job")),
        )
        self._update_worker_log_panel()

    def _matches_worker_filters(self, worker: dict) -> bool:
        status_filter = str(self.worker_status_filter.currentData() or "all")
        if status_filter != "all" and worker_status_state(worker) != status_filter:
            return False

        query = self.worker_search_edit.text().strip().lower()
        if not query:
            return True

        search_fields = [
            worker.get("name"),
            worker.get("ip"),
            worker.get("gpu"),
            worker.get("blenderVersion"),
            worker.get("renderDevicePreference"),
            worker.get("status"),
            worker.get("currentJobId"),
        ]
        haystack = " ".join(str(value or "").lower() for value in search_fields)
        return query in haystack

    def _current_worker_payload(self) -> dict[str, Any] | None:
        if not self.selected_worker_id:
            return None
        for worker in self._all_workers:
            if worker.get("id") == self.selected_worker_id:
                return worker
        return None

    def _on_worker_job_tab_changed(self, index: int) -> None:
        self.selected_worker_job_id = self._worker_job_tab_data(index)
        self._refresh_progress_overview(
            metrics=self._last_snapshot.get("metrics", {}),
            primary_job=self._last_snapshot.get("job"),
            jobs=self._snapshot_jobs_for_worker_views(self._last_snapshot, self._last_snapshot.get("job")),
            workers=self._all_workers,
        )
        self._refresh_worker_table_view()

    def _selected_worker_job_tab_id(self) -> str | None:
        if self.worker_job_tabs.count() <= 0:
            return self.selected_worker_job_id
        return self._worker_job_tab_data(self.worker_job_tabs.currentIndex())

    def _worker_job_tab_data(self, index: int) -> str | None:
        if index < 0 or index >= self.worker_job_tabs.count():
            return None
        value = self.worker_job_tabs.tabData(index)
        if value in {None, ""}:
            return None
        return str(value)

    def _snapshot_jobs_for_worker_views(self, snapshot: dict[str, Any], primary_job: dict[str, Any] | None) -> list[dict]:
        jobs: list[dict] = []
        seen_job_ids: set[str] = set()
        for candidate in [primary_job, *(snapshot.get("jobs") or [])]:
            if not isinstance(candidate, dict):
                continue
            job_id = str(candidate.get("jobId") or "")
            if not job_id or job_id in seen_job_ids:
                continue
            seen_job_ids.add(job_id)
            jobs.append(candidate)
        return jobs

    def _job_lookup_for_worker_views(
        self,
        jobs: list[dict[str, Any]],
        primary_job: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        lookup: dict[str, dict[str, Any]] = {}
        for candidate in [primary_job, *jobs]:
            if not isinstance(candidate, dict):
                continue
            job_id = str(candidate.get("jobId") or "")
            if not job_id or job_id in lookup:
                continue
            lookup[job_id] = candidate
        return lookup

    def _rebuild_worker_job_tabs(
        self,
        workers: list[dict[str, Any]],
        jobs: list[dict[str, Any]],
        primary_job: dict[str, Any] | None,
    ) -> None:
        previous_job_id = self._selected_worker_job_tab_id() or self.selected_worker_job_id
        worker_counts: dict[str, int] = {}
        for worker in workers:
            job_id = str(worker.get("currentJobId") or "")
            if job_id:
                worker_counts[job_id] = worker_counts.get(job_id, 0) + 1

        active_statuses = {"running", "validating", "paused", "blocked"}
        tab_jobs: list[dict[str, Any]] = []
        seen_job_ids: set[str] = set()
        for candidate in [primary_job, *jobs]:
            if not isinstance(candidate, dict):
                continue
            job_id = str(candidate.get("jobId") or "")
            status = str(candidate.get("status") or "").lower()
            if not job_id or job_id in seen_job_ids:
                continue
            if status not in active_statuses and job_id not in worker_counts:
                continue
            seen_job_ids.add(job_id)
            tab_jobs.append(candidate)

        self.worker_job_tabs.blockSignals(True)
        while self.worker_job_tabs.count() > 0:
            self.worker_job_tabs.removeTab(0)
        all_index = self.worker_job_tabs.addTab(self._t("All ({count})", count=len(workers)))
        self.worker_job_tabs.setTabData(all_index, None)
        self.worker_job_tabs.setTabToolTip(all_index, self._t("Show all registered machines."))
        for candidate in tab_jobs:
            job_id = str(candidate.get("jobId") or "")
            job_name = str(candidate.get("jobName") or job_id)
            label = f"{job_name} ({worker_counts.get(job_id, 0)})"
            index = self.worker_job_tabs.addTab(label)
            self.worker_job_tabs.setTabData(index, job_id)
            self.worker_job_tabs.setTabToolTip(
                index,
                self._t("{name}\nStatus: {status}\nWorkers in this job: {count}", name=job_name, status=job_status_view(candidate, self.language)[0], count=worker_counts.get(job_id, 0)),
            )

        available_job_ids = {str(candidate.get("jobId") or "") for candidate in tab_jobs}
        target_job_id = previous_job_id if previous_job_id in available_job_ids else None
        if previous_job_id is not None and previous_job_id not in available_job_ids and tab_jobs:
            target_job_id = str(tab_jobs[0].get("jobId") or "")

        target_index = 0
        if target_job_id:
            for index in range(self.worker_job_tabs.count()):
                if self._worker_job_tab_data(index) == target_job_id:
                    target_index = index
                    break
        self.worker_job_tabs.setCurrentIndex(target_index)
        self.worker_job_tabs.setVisible(self.worker_job_tabs.count() > 2)
        self.selected_worker_job_id = self._worker_job_tab_data(target_index)
        self.worker_job_tabs.blockSignals(False)

    def _matches_worker_job_tab(self, worker: dict[str, Any], selected_job_id: str | None) -> bool:
        if not selected_job_id:
            return True
        return str(worker.get("currentJobId") or "") == selected_job_id

    def _resolve_worker_job(
        self,
        worker: dict[str, Any],
        jobs_by_id: dict[str, dict[str, Any]],
        selected_job_id: str | None,
        fallback_job: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        worker_job_id = str(worker.get("currentJobId") or "")
        if worker_job_id and worker_job_id in jobs_by_id:
            return jobs_by_id[worker_job_id]
        if selected_job_id and selected_job_id in jobs_by_id:
            return jobs_by_id[selected_job_id]
        return fallback_job

    def _visible_queue_jobs(
        self,
        *,
        queue: list[dict[str, Any]],
        active_job: dict[str, Any] | None,
        jobs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        visible_statuses = {"queued", "running", "validating", "paused", "blocked"}
        items: list[dict[str, Any]] = []
        seen_job_ids: set[str] = set()

        for candidate in [active_job, *jobs]:
            if not isinstance(candidate, dict):
                continue
            job_id = str(candidate.get("jobId") or "")
            status = str(candidate.get("status") or "").lower()
            if not job_id or job_id in seen_job_ids or status not in visible_statuses:
                continue
            seen_job_ids.add(job_id)
            items.append(candidate)

        for candidate in queue:
            if not isinstance(candidate, dict):
                continue
            job_id = str(candidate.get("jobId") or "")
            status = str(candidate.get("status") or "").lower()
            if not job_id or job_id in seen_job_ids or status not in visible_statuses:
                continue
            seen_job_ids.add(job_id)
            items.append(candidate)

        return items

    def _refresh_progress_overview(
        self,
        *,
        metrics: dict[str, Any],
        primary_job: dict[str, Any] | None,
        jobs: list[dict[str, Any]],
        workers: list[dict[str, Any]],
    ) -> None:
        selected_job_id = self._selected_worker_job_tab_id()
        selected_job = self._job_lookup_for_worker_views(jobs, primary_job).get(selected_job_id or "")
        if selected_job_id and selected_job is not None:
            overview = self._job_overview_metrics(selected_job, workers)
        else:
            overview = self._farm_overview_metrics(metrics, primary_job, workers)

        total_frames = int(overview.get("total_frames", 0) or 0)
        completed_frames = int(overview.get("completed_frames", 0) or 0)
        rendering_frames = int(overview.get("rendering_frames", 0) or 0)
        pending_frames = int(overview.get("pending_frames", 0) or 0)
        error_frames = int(overview.get("error_frames", 0) or 0)
        online_machines = int(overview.get("machines_online", 0) or 0)
        rendering_machines = int(overview.get("machines_rendering", 0) or 0)
        overall_percent = float(overview.get("overall_percent", 0.0) or 0.0)

        self.job_card.set_value(str(overview.get("job_name") or "-"))
        self.total_frames_card.set_value(str(total_frames))
        self.rendering_frames_card.set_value(str(rendering_frames))
        self.completed_frames_card.set_value(str(completed_frames))
        self.error_frames_card.set_value(str(error_frames))
        self.pending_frames_card.set_value(str(pending_frames))
        if workers:
            self.online_machines_card.set_value(f"{online_machines} / {len(workers)}")
        else:
            self.online_machines_card.set_value(str(online_machines))
        self.rendering_machines_card.set_value(str(rendering_machines))
        self.eta_card.set_value(self._format_duration(overview.get("eta_seconds")))
        self.elapsed_card.set_value(self._format_duration(overview.get("elapsed_seconds", 0.0)))
        self.general_progress_bar.setValue(int(max(0.0, min(100.0, overall_percent))))
        self.general_progress_bar.setFormat(f"{overall_percent:.1f}%")
        self.progress_detail_label.setText(f"{completed_frames} / {total_frames} frames")

    def _farm_overview_metrics(
        self,
        metrics: dict[str, Any],
        primary_job: dict[str, Any] | None,
        workers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        overview = dict(metrics)
        overview["job_name"] = overview.get("job_name") or (primary_job or {}).get("jobName") or "-"
        overview["overall_percent"] = self._resolve_overall_progress_value(metrics, primary_job, workers)
        return overview

    def _job_overview_metrics(self, job: dict[str, Any], workers: list[dict[str, Any]]) -> dict[str, Any]:
        job_id = str(job.get("jobId") or "")
        total_frames = int(job.get("totalFrames") or job.get("total_frames") or 0)
        completed_frames = int(
            job.get("completedFramesCount")
            or job.get("completedFrameCount")
            or job.get("completed_frames")
            or 0
        )
        pending_frames = int(job.get("pending_frames") or 0)
        rendering_frames = int(job.get("rendering_frames") or 0)
        error_frames = int(job.get("error_frames") or 0)
        overall_percent = self._resolve_overall_progress_value(
            {
                "total_frames": total_frames,
                "completed_frames": completed_frames,
                "overall_percent": self._first_progress_value(
                    job.get("overall_percent"),
                    job.get("overallPercent"),
                    job.get("progressPercent"),
                ),
            },
            job,
            workers,
        )
        elapsed_seconds = self._job_elapsed_seconds(job)
        eta_seconds = self._estimate_eta_seconds(elapsed_seconds, overall_percent)
        rendering_machines = sum(
            1
            for worker in workers
            if str(worker.get("currentJobId") or "") == job_id and str(worker.get("status") or "").lower() == "rendering"
        )
        online_machines = sum(
            1
            for worker in workers
            if str(worker.get("status") or "").lower() not in {"offline", "disabled"}
        )
        return {
            "job_name": job.get("jobName") or job_id or "-",
            "total_frames": total_frames,
            "completed_frames": completed_frames,
            "pending_frames": pending_frames,
            "rendering_frames": rendering_frames,
            "error_frames": error_frames,
            "overall_percent": overall_percent,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
            "machines_online": online_machines,
            "machines_rendering": rendering_machines,
        }

    @staticmethod
    def _first_progress_value(*values: Any) -> float | None:
        for value in values:
            normalized = normalize_progress_percent(value)
            if normalized is not None:
                return normalized
        return None

    def _job_elapsed_seconds(self, job: dict[str, Any]) -> float:
        started_at = parse_datetime(str(job.get("startedAt") or "")) if job.get("startedAt") else None
        if started_at is None:
            return 0.0
        finished_at = parse_datetime(str(job.get("finishedAt") or "")) if job.get("finishedAt") else None
        if finished_at is not None:
            end_time = finished_at
        elif started_at.tzinfo is not None:
            end_time = datetime.now(started_at.tzinfo)
        else:
            end_time = datetime.now()
        return max(0.0, (end_time - started_at).total_seconds())

    @staticmethod
    def _estimate_eta_seconds(elapsed_seconds: float, overall_percent: float) -> float | None:
        normalized_percent = max(0.0, min(100.0, float(overall_percent or 0.0)))
        if elapsed_seconds <= 0.0 or normalized_percent <= 0.0 or normalized_percent >= 100.0:
            return None
        return max(0.0, elapsed_seconds * ((100.0 - normalized_percent) / normalized_percent))

    def _resolve_overall_progress_value(
        self,
        metrics: dict[str, Any],
        job: dict[str, Any] | None = None,
        workers: list[dict[str, Any]] | None = None,
    ) -> float:
        total_frames = int(metrics.get("total_frames", 0) or 0)
        completed_frames = int(metrics.get("completed_frames", 0) or 0)
        normalized = normalize_progress_percent(metrics.get("overall_percent"))
        if normalized is None:
            normalized = 0.0
        if normalized <= 0.0 and total_frames > 0 and completed_frames > 0:
            normalized = (completed_frames / total_frames) * 100.0
        if normalized <= 0.0 and total_frames > 0 and job:
            normalized = self._estimate_overall_progress_from_job(
                job=job,
                workers=workers or [],
                completed_frames=completed_frames,
                total_frames=total_frames,
            )
        return max(0.0, min(100.0, normalized))

    def _resolve_worker_progress_value(self, worker: dict[str, Any], current_block: dict[str, Any] | None) -> float:
        normalized = normalize_progress_percent(worker.get("progress"))
        if normalized is not None and normalized > 0.0:
            return max(0.0, min(100.0, normalized))

        if str(worker.get("status") or "").lower() != "rendering" or current_block is None:
            return max(0.0, min(100.0, normalized or 0.0))

        current_frame = worker.get("currentFrame")
        start_frame = current_block.get("startFrame")
        end_frame = current_block.get("endFrame")
        if start_frame is None or end_frame is None:
            return max(0.0, min(100.0, normalized or 0.0))
        if current_frame is None:
            return 50.0

        try:
            start_value = int(start_frame)
            end_value = int(end_frame)
            current_value = int(current_frame)
        except (TypeError, ValueError):
            return max(0.0, min(100.0, normalized or 0.0))

        if end_value <= start_value:
            return 50.0

        bounded_frame = min(max(current_value, start_value), end_value)
        completed_portion = (bounded_frame - start_value + 0.5) / max(1, end_value - start_value + 1)
        estimated = completed_portion * 100.0
        return max(1.0, min(99.0, estimated))

    def _resolve_current_block(
        self,
        worker: dict[str, Any],
        blocks_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        current_block_id = worker.get("currentBlockId")
        if current_block_id in blocks_by_id:
            return blocks_by_id[current_block_id]

        worker_id = worker.get("id")
        candidates = [
            block
            for block in blocks_by_id.values()
            if block.get("workerId") == worker_id and block.get("status") in {"assigned", "rendering"}
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda block: (block.get("startFrame", 0), block.get("endFrame", 0)))
        return candidates[0]

    def _estimate_overall_progress_from_job(
        self,
        *,
        job: dict[str, Any],
        workers: list[dict[str, Any]],
        completed_frames: int,
        total_frames: int,
    ) -> float:
        if total_frames <= 0:
            return 0.0

        blocks = job.get("blocks", [])
        if not isinstance(blocks, list) or not blocks:
            return 0.0

        worker_by_id = {str(worker.get("id")): worker for worker in workers if worker.get("id")}
        blocks_by_id = {
            block.get("blockId"): block
            for block in blocks
            if isinstance(block, dict) and block.get("blockId")
        }

        estimated_frames = float(completed_frames)
        if estimated_frames <= 0.0:
            estimated_frames = float(
                sum(
                    max(0, int(block.get("endFrame", 0)) - int(block.get("startFrame", 0)) + 1)
                    for block in blocks
                    if block.get("status") == "done"
                )
            )

        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("status") not in {"assigned", "rendering"}:
                continue

            worker_id = str(block.get("workerId") or "")
            worker = worker_by_id.get(worker_id)
            if worker is None:
                continue

            block_frames = max(0, int(block.get("endFrame", 0)) - int(block.get("startFrame", 0)) + 1)
            if block_frames <= 0:
                continue

            progress_percent = self._resolve_worker_progress_value(worker, self._resolve_current_block(worker, blocks_by_id))
            estimated_frames += (block_frames * progress_percent) / 100.0

        return (estimated_frames / total_frames) * 100.0

    def _build_access_item(self, value) -> QTableWidgetItem:
        if value is True:
            item = self._centered_item("OK")
            item.setForeground(QBrush(QColor("#7de06d")))
            return item
        if value is False:
            item = self._centered_item("X")
            item.setForeground(QBrush(QColor("#ff6f6f")))
            return item
        item = self._centered_item("-")
        item.setForeground(QBrush(QColor("#8d98ab")))
        return item

    def _format_worker_frame_text(self, worker: dict, job: dict | None, current_block: dict | None) -> str:
        total_frames = 0
        if job:
            start_frame = job.get("startFrame")
            end_frame = job.get("endFrame")
            if start_frame is not None and end_frame is not None:
                total_frames = max(0, int(end_frame) - int(start_frame) + 1)

        if str(worker.get("status") or "").lower() == "rendering":
            current_frame = worker.get("currentFrame")
            if current_frame is not None and total_frames:
                return f"{current_frame} / {total_frames}"
            if current_frame is not None:
                return str(current_frame)
            if current_block:
                return f"{current_block.get('startFrame', '-')} - {current_block.get('endFrame', '-')}"

        if total_frames:
            return f"0 / {total_frames}"
        return self._t("Free")

    def _format_relative_age(self, timestamp) -> str:
        return relative_age(timestamp, self.language)

    def _show_logs_dialog(self) -> None:
        worker = self._current_worker_payload()
        worker_name = str(worker.get("name")) if worker else None
        worker_log = self._worker_log_text(self.selected_worker_id) if self.selected_worker_id else ""
        dialog = LogViewerDialog(parent=self, language=self.language)
        dialog.set_logs(
            master_text=self._master_log_source,
            worker_name=worker_name,
            worker_text=worker_log,
        )
        dialog.exec()

    def _update_footer(self, snapshot: dict, metrics: dict, workers: list[dict]) -> None:
        master_name = getattr(self.settings, "master_name", "") if self.settings is not None else ""
        master_name = master_name or "MASTER"
        discovery = snapshot.get("coordinatorDiscovery", {})
        coordinator_status = str(discovery.get("status") or "")

        if self.mode == "coordinator":
            if coordinator_status == "connected":
                connection_text = self._t("{name} connected", name=master_name)
                connection_color = "#66d36e"
            elif coordinator_status == "conflict" or snapshot.get("coordinatorError"):
                connection_text = self._t("{name} has a Coordinator conflict", name=master_name)
                connection_color = "#ff6464"
            else:
                connection_text = self._t("{name} waiting for Coordinator", name=master_name)
                connection_color = "#ff9f43"
        else:
            connection_text = self._t("{name} connected", name=master_name)
            connection_color = "#66d36e"

        self.connection_indicator.setStyleSheet(f"background: {connection_color}; border-radius: 5px;")
        self.connection_status_label.setText(connection_text)
        online = int(metrics.get("machines_online", 0) or 0)
        rendering = int(metrics.get("machines_rendering", 0) or 0)
        self.footer_workers_label.setText(f"{self._t('Workers online')}: {online} / {len(workers)}")
        self.footer_rendering_label.setText(f"{self._t('Machines rendering')}: {rendering}")
        self.footer_version_label.setText(f"Master v{self.runtime_metadata['appVersion']}")
        self.local_time_label.setText(
            self._t("Local time: {value}", value=datetime.now().strftime('%d/%m/%Y %H:%M:%S'))
        )

    def _show_runtime_details(self) -> None:
        snapshot = self._last_snapshot or {}
        metrics = snapshot.get("metrics", {})
        discovery = snapshot.get("coordinatorDiscovery", {})
        coordinator = snapshot.get("coordinator", {})

        lines = [
            f"{self._t('Mode')}: {self.mode}",
            f"Master: {getattr(self.settings, 'master_name', '-') if self.settings else '-'}",
            f"{self._t('Base folder')}: {self.base_dir}",
            f"{self._t('Configuration')}: {self.persistence.config_path}",
            f"{self._t('Jobs folder')}: {self.persistence.jobs_dir}",
            f"{self._t('Logs folder')}: {self.persistence.logs_dir}",
            "",
            f"{self._t('App version')}: {self.runtime_metadata['appVersion']}",
            f"{self._t('Build ID')}: {self.runtime_metadata['buildId']}",
            f"{self._t('Protocol version')}: {self.runtime_metadata['protocolVersion']}",
            "",
            f"{self._t('Workers online')}: {metrics.get('machines_online', 0)}",
            f"{self._t('Machines rendering')}: {metrics.get('machines_rendering', 0)}",
            f"{self._t('Active job')}: {metrics.get('job_name') or '-'}",
        ]

        if self.mode == "coordinator":
            lines.extend(
                [
                    "",
                    f"{self._t('Coordinator status')}: {discovery.get('status', '-')}",
                    f"{self._t('Coordinator message')}: {discovery.get('message', '-')}",
                    f"{self._t('Coordinator host')}: {coordinator.get('hostname', '-')}",
                    f"{self._t('Coordinator API')}: {coordinator.get('apiUrl', discovery.get('activeApiUrl', '-'))}",
                ]
            )

        self._show_text_report(self._t("Settings and runtime"), "\n".join(str(line) for line in lines))

    def _build_info_pair(self, title: str, value_label: QLabel) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("captionLabel")
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        return wrapper

    def _build_footer_divider(self) -> QFrame:
        divider = QFrame()
        divider.setObjectName("footerDivider")
        divider.setFrameShape(QFrame.VLine)
        divider.setFixedHeight(16)
        return divider

    def _wrap_centered(self, widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        layout.addWidget(widget)
        layout.addStretch(1)
        return container

    def _centered_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        return item

    def _set_button_variant(self, button: QPushButton, variant: str) -> None:
        button.setProperty("variant", variant)
        button.style().unpolish(button)
        button.style().polish(button)

    def _set_button_icon(self, button: QPushButton, standard_icon_name: str) -> None:
        names = {"SP_DialogOpenButton": "folder-open", "SP_DirOpenIcon": "folder-open",
                 "SP_MediaPlay": "play", "SP_MediaPause": "pause", "SP_BrowserStop": "square",
                 "SP_BrowserReload": "refresh-cw", "SP_FileDialogDetailedView": "file-text",
                 "SP_DialogSaveButton": "save", "SP_FileDialogNewFolder": "plus",
                 "SP_DialogApplyButton": "wrench", "SP_ComputerIcon": "monitor", "SP_TrashIcon": "trash-2"}
        name = "settings" if button is getattr(self, "settings_button", None) else names.get(standard_icon_name, "file-text")
        path = resource_path(self.base_dir, "img", "icons", f"{name}_blue_20.png")
        if path.is_file():
            button.setIcon(QIcon(str(path)))
        else:
            button.setIcon(self.style().standardIcon(getattr(QStyle, standard_icon_name)))
        button.setIconSize(QSize(20, 20))
