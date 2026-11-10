from __future__ import annotations

import getpass
from pathlib import Path
import webbrowser

from PySide6.QtCore import QEasingCurve, QEvent, QParallelAnimationGroup, QPropertyAnimation, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCloseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QComboBox,
    QGraphicsOpacityEffect,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.core.frame_selection import FrameSubmissionGroup, ImageEntry, build_submission_groups, discover_image_entries
from app.core.settings import DEFAULT_COORDINATOR_URL, ViewerSettings
from app.services.coordinator_client import CoordinatorClient, CoordinatorClientError
from app.services.coordinator_discovery import CoordinatorDiscoveryService
from app.services.logger import AppLogger
from app.services.persistence import PersistenceService
from renderfarm_shared.i18n import LANGUAGE_NAMES, normalize_language, tr
from renderfarm_shared.qt_layout import scrollable


class SelectionToggleButton(QPushButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(28, 28)
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(2, 2, -2, -2)

        if self.isChecked():
            painter.setBrush(QColor("#ff972f"))
            painter.setPen(QPen(QColor("#ffb75c"), 1.3))
        else:
            painter.setBrush(QColor(6, 16, 28, 214))
            painter.setPen(QPen(QColor("#314054"), 1.3))
        painter.drawEllipse(rect)

        if self.isChecked():
            painter.setPen(QPen(QColor("#ffffff"), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            start_x = rect.left() + rect.width() * 0.27
            start_y = rect.top() + rect.height() * 0.54
            mid_x = rect.left() + rect.width() * 0.44
            mid_y = rect.top() + rect.height() * 0.70
            end_x = rect.left() + rect.width() * 0.74
            end_y = rect.top() + rect.height() * 0.33
            painter.drawLine(int(start_x), int(start_y), int(mid_x), int(mid_y))
            painter.drawLine(int(mid_x), int(mid_y), int(end_x), int(end_y))

        painter.end()


class TimelineImageViewer(QFrame):
    selectionToggled = Signal(str, bool)
    stepRequested = Signal(int)
    transitionFinished = Signal()

    _SOURCE_PIXMAP_CACHE: dict[str, QPixmap] = {}

    def __init__(self, parent: QWidget | None = None, *, language: str = "en") -> None:
        super().__init__(parent)
        self.language = normalize_language(language)
        self.setObjectName("viewerCard")
        self.setFocusPolicy(Qt.StrongFocus)

        self._entry: ImageEntry | None = None
        self._checked = False
        self._renderable = False
        self._wheel_accumulator = 0.0
        self._animation_group: QParallelAnimationGroup | None = None
        self._current_stage_size = QSize()

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        self.stage = QFrame(self)
        self.stage.setObjectName("imageStage")
        self.stage.setMinimumHeight(560)
        self.stage.installEventFilter(self)
        root.addWidget(self.stage, 1)

        self.current_label = self._build_image_label(self.stage)
        self.current_opacity = QGraphicsOpacityEffect(self.current_label)
        self.current_label.setGraphicsEffect(self.current_opacity)

        self.incoming_label = self._build_image_label(self.stage)
        self.incoming_opacity = QGraphicsOpacityEffect(self.incoming_label)
        self.incoming_label.setGraphicsEffect(self.incoming_opacity)
        self.incoming_label.hide()

        self.overlay = QWidget(self.stage)
        self.overlay.setStyleSheet("background: transparent;")
        self.overlay.installEventFilter(self)
        overlay_layout = QVBoxLayout(self.overlay)
        overlay_layout.setContentsMargins(16, 16, 16, 16)
        overlay_layout.setSpacing(0)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.badge_label = QLabel("", self.overlay)
        self.badge_label.setStyleSheet(
            "padding: 4px 11px;"
            "border-radius: 12px;"
            "background: rgba(7, 17, 29, 0.78);"
            "color: #eef4fb;"
            "font-size: 11px;"
            "font-weight: 700;"
        )
        top_row.addWidget(self.badge_label, 0, Qt.AlignLeft | Qt.AlignTop)
        top_row.addStretch(1)

        self.checkbox = SelectionToggleButton(self.overlay)
        self.checkbox.toggled.connect(self._handle_checkbox_toggled)
        top_row.addWidget(self.checkbox, 0, Qt.AlignRight | Qt.AlignTop)
        overlay_layout.addLayout(top_row)
        overlay_layout.addStretch(1)

        self.empty_state = QWidget(self.stage)
        self.empty_state.installEventFilter(self)
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(28, 28, 28, 28)
        empty_layout.setSpacing(12)
        empty_layout.addStretch(1)

        icon = QLabel("[]")
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet("color:#61738b;font-size:70px;font-weight:700;")
        empty_layout.addWidget(icon)

        self.empty_title_label = QLabel(self._t("No image to display"))
        self.empty_title_label.setObjectName("emptyStateTitle")
        self.empty_title_label.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(self.empty_title_label)

        self.empty_subtitle_label = QLabel(self._t("Select an image folder to begin."))
        self.empty_subtitle_label.setObjectName("emptyStateSubtitle")
        self.empty_subtitle_label.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(self.empty_subtitle_label)
        empty_layout.addStretch(1)

        self.meta_title_label = QLabel(self._t("Waiting for an image folder"))
        self.meta_title_label.setObjectName("viewerCardTitle")
        root.addWidget(self.meta_title_label)

        self.meta_subtitle_label = QLabel("Use a roda do mouse ou as setas esquerda/direita para percorrer os frames.")
        self.meta_subtitle_label.setObjectName("viewerMeta")
        self.meta_subtitle_label.setWordWrap(True)
        root.addWidget(self.meta_subtitle_label)

        self._layout_stage_children()
        self._show_empty_state()

    def _t(self, text: str, **values) -> str:
        return tr(text, self.language, **values)

    def eventFilter(self, watched: object, event: object) -> bool:
        if event is not None and getattr(event, "type", None):
            event_type = event.type()
            if event_type == QEvent.Wheel:
                self._handle_wheel_event(event)
                return True
            if event_type == QEvent.MouseButtonPress:
                self.setFocus(Qt.MouseFocusReason)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_stage_children()
        if self.is_animating():
            self._stop_animation(finish_current=True)
        elif self._entry and self._stage_size_changed():
            self._apply_static_entry()

    def is_animating(self) -> bool:
        return self._animation_group is not None

    def set_entry(self, entry: ImageEntry | None, *, checked: bool, direction: int = 0, animate: bool = True) -> None:
        if self.is_animating():
            self._stop_animation(finish_current=True)

        self._entry = entry
        self._checked = bool(checked)
        self._renderable = bool(entry and entry.is_renderable)
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(self._checked)
        self.checkbox.blockSignals(False)
        self.checkbox.setEnabled(self._renderable)

        if entry is None:
            self._show_empty_state()
            self.current_label.clear()
            self.current_label.hide()
            self.incoming_label.hide()
            self.badge_label.clear()
            self.meta_title_label.setText(self._t("No image to display"))
            self.meta_subtitle_label.setText(self._t("Select an image folder to begin."))
            return

        self._update_badge(entry)
        self.meta_title_label.setText(entry.path.name)
        self.meta_subtitle_label.setText(self._entry_description(entry))
        self.empty_state.hide()
        self.current_label.show()
        self.overlay.show()

        has_visible_current = self.current_label.pixmap() is not None and not self.current_label.pixmap().isNull()
        should_animate = animate and has_visible_current and direction != 0 and self._content_rect().width() > 10
        if should_animate:
            self._animate_to_entry(entry, direction)
        else:
            self._apply_static_entry()

    def set_checked(self, checked: bool) -> None:
        self._checked = bool(checked)
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(self._checked)
        self.checkbox.blockSignals(False)
        self._apply_current_opacity()

    def warm_cache(self, entries: list[ImageEntry]) -> None:
        for entry in entries:
            self._load_source_pixmap(entry)

    def _build_image_label(self, parent: QWidget) -> QLabel:
        label = QLabel(parent)
        label.setObjectName("heroImage")
        label.setAlignment(Qt.AlignCenter)
        label.setScaledContents(False)
        label.installEventFilter(self)
        return label

    def _handle_wheel_event(self, event) -> None:
        raw_delta = 0.0
        threshold = 120.0

        pixel_delta = event.pixelDelta().y()
        if pixel_delta:
            raw_delta = float(pixel_delta)
            threshold = 36.0
        else:
            angle_delta = event.angleDelta().y()
            if angle_delta:
                raw_delta = float(angle_delta)

        if not raw_delta:
            return

        self._wheel_accumulator += raw_delta
        while self._wheel_accumulator >= threshold:
            self._wheel_accumulator -= threshold
            self.stepRequested.emit(-1)
        while self._wheel_accumulator <= -threshold:
            self._wheel_accumulator += threshold
            self.stepRequested.emit(1)
        event.accept()

    def _handle_checkbox_toggled(self, checked: bool) -> None:
        self._checked = bool(checked)
        self._apply_current_opacity()
        if self._entry:
            self.selectionToggled.emit(str(self._entry.path), self._checked)

    def _show_empty_state(self) -> None:
        self.empty_state.show()
        self.overlay.hide()
        self.current_label.hide()
        self.incoming_label.hide()
        self.stage.setToolTip("")

    def _apply_static_entry(self) -> None:
        if not self._entry:
            return
        content_rect = self._content_rect()
        self.current_label.setGeometry(content_rect)
        self.current_label.setPixmap(self._build_display_pixmap(self._entry, content_rect.size()))
        self.current_label.show()
        self.incoming_label.hide()
        self.stage.setToolTip(self._tooltip_text(self._entry))
        self._apply_current_opacity()
        self._current_stage_size = content_rect.size()

    def _animate_to_entry(self, entry: ImageEntry, direction: int) -> None:
        content_rect = self._content_rect()
        offset = max(56, min(220, int(content_rect.width() * 0.16)))
        signed_offset = offset if direction > 0 else -offset
        start_rect = content_rect.translated(signed_offset, 0)
        finish_rect = content_rect.translated(-signed_offset, 0)

        self.incoming_label.setGeometry(start_rect)
        self.incoming_label.setPixmap(self._build_display_pixmap(entry, content_rect.size()))
        self.incoming_label.show()
        self.current_label.raise_()
        self.incoming_label.raise_()
        self.overlay.raise_()
        self.stage.setToolTip(self._tooltip_text(entry))

        self.current_opacity.setOpacity(self._target_opacity(selected=False, renderable=self._renderable))
        self.incoming_opacity.setOpacity(0.48)

        group = QParallelAnimationGroup(self)
        for target, start_value, end_value in (
            (self.current_label, content_rect, finish_rect),
            (self.incoming_label, start_rect, content_rect),
        ):
            animation = QPropertyAnimation(target, b"geometry", group)
            animation.setDuration(150)
            animation.setStartValue(start_value)
            animation.setEndValue(end_value)
            animation.setEasingCurve(QEasingCurve.OutCubic)
            group.addAnimation(animation)

        current_opacity_animation = QPropertyAnimation(self.current_opacity, b"opacity", group)
        current_opacity_animation.setDuration(135)
        current_opacity_animation.setStartValue(self.current_opacity.opacity())
        current_opacity_animation.setEndValue(0.16)
        current_opacity_animation.setEasingCurve(QEasingCurve.OutCubic)
        group.addAnimation(current_opacity_animation)

        incoming_opacity_animation = QPropertyAnimation(self.incoming_opacity, b"opacity", group)
        incoming_opacity_animation.setDuration(150)
        incoming_opacity_animation.setStartValue(0.48)
        incoming_opacity_animation.setEndValue(self._target_opacity(selected=self._checked, renderable=self._renderable))
        incoming_opacity_animation.setEasingCurve(QEasingCurve.OutCubic)
        group.addAnimation(incoming_opacity_animation)

        group.finished.connect(self._finalize_transition)
        self._animation_group = group
        group.start()

    def _finalize_transition(self) -> None:
        self.current_label, self.incoming_label = self.incoming_label, self.current_label
        self.current_opacity, self.incoming_opacity = self.incoming_opacity, self.current_opacity
        self.incoming_label.hide()
        self.incoming_label.clear()
        self.current_label.setGeometry(self._content_rect())
        self._apply_current_opacity()
        self._animation_group = None
        self._current_stage_size = self._content_rect().size()
        self.transitionFinished.emit()

    def _stop_animation(self, *, finish_current: bool) -> None:
        if self._animation_group is None:
            return
        self._animation_group.stop()
        self._animation_group = None
        if finish_current and self._entry:
            self.incoming_label.hide()
            self.incoming_label.clear()
            self._apply_static_entry()
        self.transitionFinished.emit()

    def _apply_current_opacity(self) -> None:
        self.current_opacity.setOpacity(self._target_opacity(selected=self._checked, renderable=self._renderable))

    def _target_opacity(self, *, selected: bool, renderable: bool) -> float:
        if not renderable:
            return 0.44
        if selected:
            return 0.78
        return 1.0

    def _content_rect(self) -> QRect:
        return self.stage.rect().adjusted(0, 0, 0, 0)

    def _layout_stage_children(self) -> None:
        stage_rect = self.stage.rect()
        self.current_label.setGeometry(stage_rect)
        self.incoming_label.setGeometry(stage_rect)
        self.overlay.setGeometry(stage_rect)
        self.empty_state.setGeometry(stage_rect)
        self.overlay.raise_()

    def _stage_size_changed(self) -> bool:
        return self._content_rect().size() != self._current_stage_size

    def _build_display_pixmap(self, entry: ImageEntry, target_size: QSize) -> QPixmap:
        safe_width = max(target_size.width(), 32)
        safe_height = max(target_size.height(), 32)
        canvas = QPixmap(safe_width, safe_height)
        canvas.fill(Qt.transparent)

        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        rect = canvas.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)
        painter.fillPath(path, QColor("#0f1620"))
        painter.setClipPath(path)

        source = self._load_source_pixmap(entry)
        if source.isNull():
            painter.fillPath(path, QColor("#121b28"))
            painter.setPen(QColor("#8ea3bf"))
            painter.drawText(rect, Qt.AlignCenter, self._t("Preview unavailable"))
        else:
            scaled = source.scaled(
                rect.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            x = rect.x() + (rect.width() - scaled.width()) // 2
            y = rect.y() + (rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)

        painter.setClipping(False)
        painter.setPen(QPen(QColor("#263244"), 1))
        painter.drawPath(path)
        if entry.error:
            painter.fillPath(path, QColor(79, 18, 18, 76))
        painter.end()
        return canvas

    def _load_source_pixmap(self, entry: ImageEntry) -> QPixmap:
        cache_key = str(entry.path)
        cached = self._SOURCE_PIXMAP_CACHE.get(cache_key)
        if cached is not None:
            return cached
        pixmap = QPixmap(cache_key)
        self._SOURCE_PIXMAP_CACHE[cache_key] = pixmap
        return pixmap

    def _update_badge(self, entry: ImageEntry) -> None:
        if entry.frame_number is not None:
            self.badge_label.setText(f"Frame {entry.frame_number}")
        else:
            self.badge_label.setText(self._t("No frame"))
        if entry.error:
            self.badge_label.setStyleSheet(
                "padding: 4px 11px;"
                "border-radius: 12px;"
                "background: rgba(86, 25, 25, 0.84);"
                "color: #ffd6d2;"
                "font-size: 11px;"
                "font-weight: 700;"
            )
        else:
            self.badge_label.setStyleSheet(
                "padding: 4px 11px;"
                "border-radius: 12px;"
                "background: rgba(7, 17, 29, 0.78);"
                "color: #eef4fb;"
                "font-size: 11px;"
                "font-weight: 700;"
            )

    def _entry_description(self, entry: ImageEntry) -> str:
        details: list[str] = []
        if entry.frame_number is not None:
            details.append(f"Frame {entry.frame_number}")
        if entry.pattern_name:
            details.append(self._t("Pattern {pattern}", pattern=entry.pattern_name))
        if entry.error:
            details.append(entry.error)
        details.append(str(entry.path))
        return "  |  ".join(details)

    def _tooltip_text(self, entry: ImageEntry) -> str:
        parts = [entry.path.name]
        if entry.frame_number is not None:
            parts.append(f"Frame: {entry.frame_number}")
        if entry.pattern_name:
            parts.append(self._t("Pattern {pattern}", pattern=entry.pattern_name))
        if entry.error:
            parts.append(entry.error)
        parts.append(str(entry.path))
        return "\n".join(parts)


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        base_dir: Path,
        settings: ViewerSettings,
        persistence: PersistenceService,
        logger: AppLogger,
    ) -> None:
        super().__init__()
        self.base_dir = base_dir
        self.settings = settings
        self.language = normalize_language(settings.language)
        self.persistence = persistence
        self.logger = logger
        self.discovery_service = CoordinatorDiscoveryService()
        self.entries: list[ImageEntry] = []
        self.selected_paths: set[str] = set()
        self.detected_coordinator_url: str | None = None
        self.current_index = -1
        self._pending_steps = 0
        self._suppress_timeline_signal = False

        self.setWindowTitle("BlenderImageViewer")
        self.setMinimumSize(720, 440)
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1580, available.width() - 40), min(920, available.height() - 50))

        self._build_layout()
        self._connect_signals()
        self._restore_settings()
        self._refresh_viewer(animate=False)
        self._update_summary()
        self._update_actions()
        QTimer.singleShot(160, lambda: self._detect_coordinator(show_dialog=False, update_status=True))

    def _t(self, text: str, **values) -> str:
        return tr(text, self.language, **values)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_settings()
        self.logger.close()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        focused_widget = QApplication.focusWidget()
        if isinstance(focused_widget, QLineEdit):
            super().keyPressEvent(event)
            return

        if event.key() == Qt.Key_Right:
            self._request_navigation(1)
            event.accept()
            return
        if event.key() == Qt.Key_Left:
            self._request_navigation(-1)
            event.accept()
            return
        super().keyPressEvent(event)

    def _build_layout(self) -> None:
        root = QWidget(self)
        root.setObjectName("centralRoot")
        self.content_scroll = scrollable(root, min_width=850)
        self.setCentralWidget(self.content_scroll)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(12)

        outer.addWidget(self._build_header())
        outer.addWidget(self._build_content(), 1)
        outer.addWidget(self._build_footer())

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("actionStrip")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(6)

        title_row = QHBoxLayout()
        title = QLabel(self._t("Image Viewer"))
        title.setObjectName("heroTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.language_combo = QComboBox()
        self.language_combo.setFixedWidth(112)
        for code in ("en", "pt", "es"):
            self.language_combo.addItem(LANGUAGE_NAMES[code], code)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(self.language)))
        self.language_combo.currentIndexChanged.connect(self._on_language_selected)
        title_row.addWidget(self.language_combo)
        layout.addLayout(title_row)

        subtitle = QLabel(
            self._t("Rendered frame review and comparison")
        )
        subtitle.setObjectName("heroSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        credit = QLabel(
            '<a href="https://novaxd.tech/" style="color:#76c4ff;text-decoration:none;">By Vinicius D\'Arcadia Cruz - https://novaxd.tech/</a>'
        )
        credit.setOpenExternalLinks(True)
        credit.setTextFormat(Qt.RichText)
        layout.addWidget(credit)
        return frame

    def _on_language_selected(self, _index: int) -> None:
        language = normalize_language(self.language_combo.currentData())
        if language == self.language:
            return
        image_folder = self.image_folder_edit.text()
        blend_file = self.blend_file_edit.text()
        output_folder = self.output_folder_edit.text()
        coordinator_readout = self.coordinator_readout.text()
        self.language = language
        self.settings.language = language
        self.persistence.save_settings(self.settings)
        self._build_layout()
        self._connect_signals()
        self.image_folder_edit.setText(image_folder)
        self.blend_file_edit.setText(blend_file)
        self.output_folder_edit.setText(output_folder)
        self.coordinator_readout.setText(
            coordinator_readout if self.detected_coordinator_url
            else self._t("No Coordinator detected automatically.")
        )
        self._refresh_viewer(animate=False)
        self._update_summary()
        self._update_actions()
        if self.entries:
            self._set_status_message(self._t("{count} image(s) found in {folder}.",
                                             count=len(self.entries), folder=Path(image_folder).name))

    def _build_content(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.sidebar = self._build_sidebar()
        self.sidebar_scroll = scrollable(self.sidebar)
        self.sidebar_scroll.setFixedWidth(320)
        layout.addWidget(self.sidebar_scroll, 0)
        layout.addWidget(self._build_viewer_panel(), 1)
        return container

    def _build_sidebar(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("sidePanel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        sources = QGroupBox(self._t("Sources and destination"))
        source_layout = QVBoxLayout(sources)
        source_layout.setSpacing(10)

        self.image_folder_edit = QLineEdit()
        self.image_folder_edit.setPlaceholderText(self._t("Image folder"))
        self.image_folder_edit.setClearButtonEnabled(True)
        source_layout.addWidget(self._caption(self._t("Image folder")))
        source_layout.addWidget(self.image_folder_edit)

        image_buttons = QHBoxLayout()
        image_buttons.setSpacing(8)
        self.open_folder_button = QPushButton(self._t("Open folder"))
        self.reload_folder_button = QPushButton(self._t("Refresh"))
        image_buttons.addWidget(self.open_folder_button)
        image_buttons.addWidget(self.reload_folder_button)
        source_layout.addLayout(image_buttons)

        self.blend_file_edit = QLineEdit()
        self.blend_file_edit.setPlaceholderText(self._t("Blend file"))
        self.blend_file_edit.setReadOnly(True)
        source_layout.addWidget(self._caption(self._t("Blend file")))
        source_layout.addWidget(self.blend_file_edit)
        self.blend_button = QPushButton(self._t("Select .blend"))
        source_layout.addWidget(self.blend_button)

        self.output_folder_edit = QLineEdit()
        self.output_folder_edit.setPlaceholderText(self._t("Output folder"))
        self.output_folder_edit.setReadOnly(True)
        source_layout.addWidget(self._caption(self._t("Output folder")))
        source_layout.addWidget(self.output_folder_edit)
        self.output_button = QPushButton(self._t("Select output folder"))
        source_layout.addWidget(self.output_button)

        coordinator = QGroupBox("Coordinator")
        coordinator_layout = QVBoxLayout(coordinator)
        coordinator_layout.setSpacing(10)

        self.coordinator_readout = QLineEdit(self._t("Detecting automatically..."))
        self.coordinator_readout.setReadOnly(True)
        coordinator_layout.addWidget(self.coordinator_readout)

        self.redetect_button = QPushButton(self._t("Refresh detection"))
        self.open_dashboard_button = QPushButton(self._t("Open web dashboard"))
        self.open_dashboard_button.setProperty("variant", "info")
        coordinator_layout.addWidget(self.redetect_button)
        coordinator_layout.addWidget(self.open_dashboard_button)

        navigation = QGroupBox(self._t("Navigation"))
        navigation_layout = QVBoxLayout(navigation)
        navigation_layout.setSpacing(8)
        navigation_layout.addWidget(self._caption(self._t("Controls")))
        hint = QLabel(self._t("Use the mouse wheel or arrow keys to browse frames."))
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        navigation_layout.addWidget(hint)

        layout.addWidget(sources)
        layout.addWidget(coordinator)
        layout.addWidget(navigation)
        layout.addStretch(1)
        return frame

    def _build_viewer_panel(self) -> QWidget:
        panel = QGroupBox(self._t("Image Viewer"))
        layout = QVBoxLayout(panel)
        layout.setSpacing(12)

        top_bar = QFrame()
        top_bar.setObjectName("infoStrip")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(14, 12, 14, 12)
        top_layout.setSpacing(12)

        self.position_label = QLabel("0 / 0")
        self.position_label.setObjectName("statusValue")
        top_layout.addWidget(self.position_label)

        top_layout.addStretch(1)

        self.summary_label = QLabel(self._t("{images} images   {selected} selected   {renderable} renderable", images=0, selected=0, renderable=0))
        self.summary_label.setObjectName("statusValue")
        self.summary_label.setWordWrap(True)
        top_layout.addWidget(self.summary_label)
        layout.addWidget(top_bar)

        self.viewer = TimelineImageViewer(language=self.language)
        self.viewer.setMinimumSize(420, 200)
        layout.addWidget(self.viewer, 1)

        timeline_bar = QFrame()
        timeline_bar.setObjectName("infoStrip")
        timeline_layout = QHBoxLayout(timeline_bar)
        timeline_layout.setContentsMargins(14, 12, 14, 12)
        timeline_layout.setSpacing(12)

        timeline_layout.addWidget(self._caption(self._t("Timeline")))
        self.timeline_slider = QSlider(Qt.Horizontal)
        self.timeline_slider.setRange(0, 0)
        self.timeline_slider.setTracking(True)
        self.timeline_slider.setSingleStep(1)
        self.timeline_slider.setPageStep(1)
        timeline_layout.addWidget(self.timeline_slider, 1)

        self.timeline_hint_label = QLabel(self._t("Mouse wheel or arrows"))
        self.timeline_hint_label.setObjectName("mutedLabel")
        timeline_layout.addWidget(self.timeline_hint_label)
        layout.addWidget(timeline_bar)
        return panel

    def _build_footer(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("footerBar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        self.status_message_label = QLabel(self._t("Select an image folder to begin."))
        self.status_message_label.setObjectName("mutedLabel")
        self.status_message_label.setWordWrap(True)
        layout.addWidget(self.status_message_label, 1)

        self.select_all_button = QPushButton(self._t("Select all"))
        self.clear_selection_button = QPushButton(self._t("Clear selection"))
        self.render_button = QPushButton(self._t("Render selected"))
        self.render_button.setProperty("variant", "primary")
        layout.addWidget(self.select_all_button)
        layout.addWidget(self.clear_selection_button)
        layout.addWidget(self.render_button)
        return frame

    def _connect_signals(self) -> None:
        self.open_folder_button.clicked.connect(self._choose_image_folder)
        self.reload_folder_button.clicked.connect(self._load_image_folder_from_field)
        self.blend_button.clicked.connect(self._choose_blend_file)
        self.output_button.clicked.connect(self._choose_output_folder)
        self.redetect_button.clicked.connect(lambda: self._detect_coordinator(show_dialog=True, update_status=True))
        self.open_dashboard_button.clicked.connect(self._open_dashboard)
        self.select_all_button.clicked.connect(self._select_all_renderable)
        self.clear_selection_button.clicked.connect(self._clear_selection)
        self.render_button.clicked.connect(self._render_selected)

        self.image_folder_edit.editingFinished.connect(self._load_image_folder_from_field)
        self.blend_file_edit.editingFinished.connect(self._save_settings)
        self.output_folder_edit.editingFinished.connect(self._save_settings)
        self.timeline_slider.valueChanged.connect(self._handle_timeline_changed)

        self.viewer.selectionToggled.connect(self._handle_viewer_selection_toggled)
        self.viewer.stepRequested.connect(self._request_navigation)
        self.viewer.transitionFinished.connect(self._consume_pending_navigation)

    def _restore_settings(self) -> None:
        self.image_folder_edit.setText(self.settings.last_image_folder)
        self.blend_file_edit.setText(self.settings.last_blend_file)
        self.output_folder_edit.setText(self.settings.last_output_folder)
        if self.settings.last_image_folder:
            self._load_image_folder_from_field()

    def _save_settings(self) -> None:
        self.settings = ViewerSettings(
            language=self.language,
            last_image_folder=self.image_folder_edit.text().strip(),
            last_blend_file=self.blend_file_edit.text().strip(),
            last_output_folder=self.output_folder_edit.text().strip(),
            grid_columns=self.settings.grid_columns,
        )
        self.persistence.save_settings(self.settings)

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("captionLabel")
        return label

    def _choose_image_folder(self) -> None:
        start_dir = self.image_folder_edit.text().strip() or self.settings.last_image_folder or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, self._t("Select image folder"), start_dir)
        if not folder:
            return
        self.image_folder_edit.setText(folder)
        self._load_image_folder_from_field()

    def _choose_blend_file(self) -> None:
        start_dir = self.blend_file_edit.text().strip() or self.image_folder_edit.text().strip() or str(Path.home())
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self._t("Select .blend"),
            start_dir,
            "Blender Files (*.blend)",
        )
        if not file_path:
            return
        self.blend_file_edit.setText(file_path)
        self._save_settings()
        self._update_actions()

    def _choose_output_folder(self) -> None:
        start_dir = self.output_folder_edit.text().strip() or self.image_folder_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, self._t("Select output folder dialog"), start_dir)
        if not folder:
            return
        self.output_folder_edit.setText(folder)
        self._save_settings()
        self._update_actions()

    def _load_image_folder_from_field(self) -> None:
        folder_text = self.image_folder_edit.text().strip()
        folder = Path(folder_text) if folder_text else None
        previous_path = str(self._current_entry().path) if self._current_entry() else ""
        self._pending_steps = 0

        if not folder or not folder.exists() or not folder.is_dir():
            self.entries = []
            self.selected_paths.clear()
            self.current_index = -1
            self._refresh_viewer(animate=False)
            self._set_status_message(self._t("Invalid or empty image folder."))
            self._save_settings()
            return

        self.entries = discover_image_entries(folder)
        valid_paths = {str(entry.path) for entry in self.entries}
        self.selected_paths = {path for path in self.selected_paths if path in valid_paths}

        if previous_path and previous_path in valid_paths:
            self.current_index = next(
                (index for index, entry in enumerate(self.entries) if str(entry.path) == previous_path),
                0,
            )
        elif self.entries:
            self.current_index = 0
        else:
            self.current_index = -1

        self._refresh_viewer(animate=False)
        self._save_settings()
        self._set_status_message(self._t("{count} image(s) found in {folder}.", count=len(self.entries), folder=folder.name))

    def _current_entry(self) -> ImageEntry | None:
        if 0 <= self.current_index < len(self.entries):
            return self.entries[self.current_index]
        return None

    def _refresh_viewer(self, *, direction: int = 0, animate: bool = True) -> None:
        entry = self._current_entry()
        checked = bool(entry and str(entry.path) in self.selected_paths)
        self.viewer.set_entry(entry, checked=checked, direction=direction, animate=animate)
        self.viewer.setFocus(Qt.OtherFocusReason)
        self._update_timeline_controls()
        self._update_summary()
        self._update_actions()
        self._prefetch_neighbors()

    def _prefetch_neighbors(self) -> None:
        if not self.entries or self.current_index < 0:
            return
        neighbor_entries: list[ImageEntry] = []
        for offset in (1, 2, -1, -2):
            target_index = self.current_index + offset
            if 0 <= target_index < len(self.entries):
                neighbor_entries.append(self.entries[target_index])
        if neighbor_entries:
            self.viewer.warm_cache(neighbor_entries)

    def _update_timeline_controls(self) -> None:
        total_entries = len(self.entries)
        current_entry = self._current_entry()
        if total_entries and current_entry is not None:
            human_index = self.current_index + 1
            frame_display = f"Frame {current_entry.frame_number}" if current_entry.frame_number is not None else self._t("No frame")
            self.position_label.setText(f"{human_index} / {total_entries}")
            self.timeline_hint_label.setText(frame_display)
            self._suppress_timeline_signal = True
            self.timeline_slider.setRange(0, max(total_entries - 1, 0))
            self.timeline_slider.setValue(self.current_index)
            self._suppress_timeline_signal = False
        else:
            self.position_label.setText("0 / 0")
            self.timeline_hint_label.setText(self._t("Mouse wheel or arrows"))
            self._suppress_timeline_signal = True
            self.timeline_slider.setRange(0, 0)
            self.timeline_slider.setValue(0)
            self._suppress_timeline_signal = False

    def _handle_timeline_changed(self, value: int) -> None:
        if self._suppress_timeline_signal or not self.entries:
            return
        self._jump_to_index(int(value))

    def _jump_to_index(self, target_index: int) -> None:
        if not self.entries:
            return
        bounded_index = max(0, min(target_index, len(self.entries) - 1))
        if bounded_index == self.current_index:
            return
        self._pending_steps = 0
        direction = 1 if bounded_index > self.current_index else -1
        animate = abs(bounded_index - self.current_index) <= 1
        self.current_index = bounded_index
        self._refresh_viewer(direction=direction, animate=animate)

    def _request_navigation(self, step: int) -> None:
        if not self.entries or step == 0:
            return
        if self.viewer.is_animating():
            self._pending_steps += 1 if step > 0 else -1
            self._pending_steps = max(-30, min(30, self._pending_steps))
            return
        self._navigate(step)

    def _navigate(self, step: int) -> None:
        if not self.entries or step == 0:
            return
        target_index = max(0, min(self.current_index + step, len(self.entries) - 1))
        if target_index == self.current_index:
            return
        direction = 1 if step > 0 else -1
        self.current_index = target_index
        self._refresh_viewer(direction=direction, animate=True)

    def _consume_pending_navigation(self) -> None:
        if self.viewer.is_animating() or self._pending_steps == 0:
            return
        step = 1 if self._pending_steps > 0 else -1
        self._pending_steps -= step
        self._navigate(step)

    def _handle_viewer_selection_toggled(self, path: str, checked: bool) -> None:
        if checked:
            self.selected_paths.add(path)
        else:
            self.selected_paths.discard(path)
        self._update_summary()
        self._update_actions()

    def _select_all_renderable(self) -> None:
        for entry in self.entries:
            if entry.is_renderable:
                self.selected_paths.add(str(entry.path))
        current_entry = self._current_entry()
        if current_entry:
            self.viewer.set_checked(str(current_entry.path) in self.selected_paths)
        self._update_summary()
        self._update_actions()

    def _clear_selection(self) -> None:
        self.selected_paths.clear()
        current_entry = self._current_entry()
        if current_entry:
            self.viewer.set_checked(False)
        self._update_summary()
        self._update_actions()

    def _update_summary(self) -> None:
        renderable_count = sum(1 for entry in self.entries if entry.is_renderable)
        selected_count = sum(1 for entry in self.entries if entry.is_renderable and str(entry.path) in self.selected_paths)
        self.summary_label.setText(self._t(
            "{images} images   {selected} selected   {renderable} renderable",
            images=len(self.entries), selected=selected_count, renderable=renderable_count,
        ))

    def _update_actions(self) -> None:
        has_entries = bool(self.entries)
        has_selection = bool(self.selected_paths)
        self.select_all_button.setEnabled(any(entry.is_renderable for entry in self.entries))
        self.clear_selection_button.setEnabled(has_selection)
        self.render_button.setEnabled(has_selection and self._has_required_render_inputs())
        self.open_dashboard_button.setEnabled(bool(self.detected_coordinator_url))
        self.reload_folder_button.setEnabled(has_entries or bool(self.image_folder_edit.text().strip()))
        self.timeline_slider.setEnabled(has_entries)

    def _has_required_render_inputs(self) -> bool:
        blend_file = self.blend_file_edit.text().strip()
        output_folder = self.output_folder_edit.text().strip()
        return bool(blend_file and output_folder)

    def _set_status_message(self, message: str) -> None:
        self.status_message_label.setText(message)
        if message.strip():
            self.logger.info(message)

    def _detect_coordinator(self, *, show_dialog: bool, update_status: bool) -> bool:
        if update_status:
            self.coordinator_readout.setText(self._t("Detecting automatically..."))
            QApplication.processEvents()

        candidate_urls: list[str] = []
        try:
            announcements = self.discovery_service.probe(force=True, wait_seconds=0.65)
        except Exception as exc:
            announcements = []
            self.logger.warning(f"Falha durante discovery do coordinator: {exc}")

        for announcement in announcements:
            if announcement.enabled and announcement.api_urls:
                candidate_urls.extend(announcement.api_urls)
        candidate_urls.append(DEFAULT_COORDINATOR_URL)

        seen_urls: set[str] = set()
        for candidate_url in candidate_urls:
            normalized_url = str(candidate_url or "").strip().rstrip("/")
            if not normalized_url or normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            try:
                payload = CoordinatorClient(normalized_url).health()
            except CoordinatorClientError:
                continue
            self.detected_coordinator_url = normalized_url
            coordinator_id = payload.get("coordinatorId") or "coordinator"
            self.coordinator_readout.setText(f"{coordinator_id} | {normalized_url}")
            self._update_actions()
            if update_status:
                self._set_status_message(self._t("Coordinator detected automatically."))
            return True

        self.detected_coordinator_url = None
        self.coordinator_readout.setText(self._t("No Coordinator detected automatically."))
        self._update_actions()
        if update_status:
            self._set_status_message(self._t("No Coordinator is available right now."))
        if show_dialog:
            QMessageBox.warning(
                self,
                self._t("Coordinator not found"),
                self._t("An active Coordinator could not be found automatically on the network."),
            )
        return False

    def _ensure_coordinator(self) -> str | None:
        if self.detected_coordinator_url:
            return self.detected_coordinator_url
        if self._detect_coordinator(show_dialog=False, update_status=True):
            return self.detected_coordinator_url
        return None

    def _open_dashboard(self) -> None:
        coordinator_url = self._ensure_coordinator()
        if not coordinator_url:
            QMessageBox.warning(self, self._t("Coordinator not found"), self._t("No Coordinator was detected to open the dashboard."))
            return
        webbrowser.open(f"{coordinator_url}/dashboard")

    def _selected_entries(self) -> list[ImageEntry]:
        return [entry for entry in self.entries if str(entry.path) in self.selected_paths]

    def _render_selected(self) -> None:
        selected_entries = self._selected_entries()
        if not selected_entries:
            QMessageBox.information(self, self._t("No selection"), self._t("Select at least one image to resubmit."))
            return

        blend_path = Path(self.blend_file_edit.text().strip())
        if not str(blend_path).lower().endswith(".blend"):
            QMessageBox.warning(self, self._t("Invalid blend file"), self._t("Select a valid .blend file."))
            return

        output_folder = Path(self.output_folder_edit.text().strip())
        if not self.output_folder_edit.text().strip():
            QMessageBox.warning(self, self._t("Invalid destination"), self._t("Select an output folder for the rerender."))
            return

        coordinator_url = self._ensure_coordinator()
        if not coordinator_url:
            QMessageBox.warning(
                self,
                self._t("Coordinator not found"),
                self._t("A Coordinator could not be detected automatically to submit the jobs."),
            )
            return

        groups, skipped = build_submission_groups(selected_entries)
        if not groups:
            QMessageBox.warning(
                self,
                self._t("Nothing renderable"),
                self._t("None of the selected images has a recognizable frame number in its file name."),
            )
            return

        try:
            output_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, self._t("Invalid destination"), self._t("Could not prepare the output folder: {error}", error=exc))
            return

        client = CoordinatorClient(coordinator_url)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for group in groups:
                payload = self._build_job_payload(group, blend_path=blend_path, output_folder=output_folder)
                client.create_job(payload)
        except CoordinatorClientError as exc:
            QMessageBox.critical(self, self._t("Failed to submit jobs"), str(exc))
            self._set_status_message(self._t("Failed to submit jobs to the Coordinator: {error}", error=exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        for entry in selected_entries:
            if entry.is_renderable:
                self.selected_paths.discard(str(entry.path))
        current_entry = self._current_entry()
        if current_entry:
            self.viewer.set_checked(str(current_entry.path) in self.selected_paths)
        self._update_summary()
        self._update_actions()
        self._save_settings()

        skipped_count = len(skipped)
        message = self._t(
            "{jobs} GPU-only job(s) submitted to rerender {frames} frame(s).",
            jobs=len(groups),
            frames=sum(len(group.frame_numbers) for group in groups),
        )
        if skipped_count:
            message += self._t(
                " {count} selected image(s) were skipped because no frame number was detected.",
                count=skipped_count,
            )
        self._set_status_message(message)
        QMessageBox.information(self, self._t("Jobs submitted"), message)

    def _build_job_payload(
        self,
        group: FrameSubmissionGroup,
        *,
        blend_path: Path,
        output_folder: Path,
    ) -> dict[str, object]:
        pattern_stem = Path(group.pattern_name).stem
        user_name = getpass.getuser().strip() or "unknown"
        return {
            "jobName": f"Rerender {blend_path.stem} | {pattern_stem} | {group.start_frame}-{group.end_frame}",
            "blendFile": str(blend_path),
            "outputFolder": str(output_folder),
            "outputPattern": group.output_pattern(output_folder),
            "startFrame": group.start_frame,
            "endFrame": group.end_frame,
            "blockSize": 1,
            "format": group.format_name,
            "ignoreRenderedFrames": False,
            "autoAddWorkers": True,
            "redistributeOfflineBlocks": True,
            "legacyUseGpu": True,
            "gpuOnlyWorkers": True,
            "submittedByMasterId": "blender_image_viewer",
            "submittedByMasterName": "BlenderImageViewer",
            "submittedByUser": user_name,
        }
