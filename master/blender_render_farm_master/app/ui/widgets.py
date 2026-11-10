from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QPainter
from renderfarm_shared.i18n import tr
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


STATUS_COLORS = {
    "green": "#66d36e",
    "blue": "#3eb2ff",
    "orange": "#ff9f43",
    "yellow": "#f3c969",
    "red": "#ff6464",
    "gray": "#8d98ab",
}

JOB_STATUS_LABELS = {
    "running": ("Rendering", STATUS_COLORS["blue"]),
    "paused": ("Paused", STATUS_COLORS["yellow"]),
    "queued": ("Waiting", STATUS_COLORS["gray"]),
    "completed": ("Completed", STATUS_COLORS["green"]),
    "done": ("Completed", STATUS_COLORS["green"]),
    "error": ("Error", STATUS_COLORS["red"]),
    "blocked": ("Blocked", STATUS_COLORS["orange"]),
    "cancelled": ("Cancelled", STATUS_COLORS["gray"]),
    "canceled": ("Cancelled", STATUS_COLORS["gray"]),
}

ACCENT_COLORS = {
    "neutral": "#eaf1fb",
    "blue": "#46b8ff",
    "green": "#7de06d",
    "orange": "#ffab4f",
    "red": "#ff6f6f",
}


class BlendFileLineEdit(QLineEdit):
    fileDropped = Signal(str)

    def __init__(self, parent=None, *, language="en") -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText(tr("Drop a .blend file here or use the browse button.", language))

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".blend"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".blend"):
                self.setText(path)
                self.fileDropped.emit(path)
                event.acceptProposedAction()
                return
        event.ignore()


class WorkerDialog(QDialog):
    def __init__(self, default_port: int, parent=None, *, language="en") -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Add worker", language))
        self.setModal(True)
        self.resize(360, 180)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("PC-RENDER-02")
        self.ip_edit = QLineEdit()
        self.ip_edit.setPlaceholderText("192.168.0.120")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(default_port)
        self.enabled_check = QCheckBox(tr("Worker enabled", language))
        self.enabled_check.setChecked(True)

        form_layout = QFormLayout()
        form_layout.addRow(tr("Name", language), self.name_edit)
        form_layout.addRow("IP", self.ip_edit)
        form_layout.addRow(tr("Port", language), self.port_spin)
        form_layout.addRow("", self.enabled_check)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.button(QDialogButtonBox.Cancel).setText(tr("Cancel", language))
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form_layout)
        layout.addWidget(button_box)

    def values(self) -> dict[str, str | int | bool]:
        return {
            "name": self.name_edit.text().strip(),
            "ip": self.ip_edit.text().strip(),
            "port": self.port_spin.value(),
            "enabled": self.enabled_check.isChecked(),
        }


class TextReportDialog(QDialog):
    def __init__(self, title: str, body: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(760, 520)

        report_edit = QPlainTextEdit()
        report_edit.setReadOnly(True)
        report_edit.setPlainText(body)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok)
        button_box.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(report_edit)
        layout.addWidget(button_box)


class LogViewerDialog(QDialog):
    def __init__(self, parent=None, *, language="en") -> None:
        super().__init__(parent)
        self.language = language
        self.setWindowTitle(tr("Logs", language))
        self.setModal(True)
        self.resize(1080, 700)
        self._master_source = ""
        self._worker_source = ""

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addStretch(1)

        toolbar.addWidget(QLabel(tr("Level", self.language)))
        self.level_combo = QComboBox()
        self.level_combo.addItem(tr("All levels", self.language), "all")
        self.level_combo.addItem(tr("Info", self.language), "INFO")
        self.level_combo.addItem(tr("Warning", self.language), "WARNING")
        self.level_combo.addItem(tr("Error", self.language), "ERROR")
        self.level_combo.currentIndexChanged.connect(self._refresh_views)
        toolbar.addWidget(self.level_combo)

        tabs = QTabWidget()

        master_tab = QWidget()
        master_layout = QVBoxLayout(master_tab)
        master_layout.setContentsMargins(8, 8, 8, 8)
        self.master_edit = QPlainTextEdit()
        self.master_edit.setReadOnly(True)
        self.master_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        master_layout.addWidget(self.master_edit)

        worker_tab = QWidget()
        worker_layout = QVBoxLayout(worker_tab)
        worker_layout.setContentsMargins(8, 8, 8, 8)
        self.worker_title = QLabel(tr("Worker log: no worker selected", self.language))
        self.worker_edit = QPlainTextEdit()
        self.worker_edit.setReadOnly(True)
        self.worker_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        worker_layout.addWidget(self.worker_title)
        worker_layout.addWidget(self.worker_edit, 1)

        tabs.addTab(master_tab, tr("Master log", self.language))
        tabs.addTab(worker_tab, tr("Worker log", self.language))

        button_box = QDialogButtonBox(QDialogButtonBox.Close)
        button_box.button(QDialogButtonBox.Close).setText(tr("Close", self.language))
        button_box.rejected.connect(self.reject)
        button_box.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(tabs, 1)
        layout.addWidget(button_box)

    def set_logs(self, master_text: str, worker_name: str | None, worker_text: str) -> None:
        self._master_source = master_text or ""
        self._worker_source = worker_text or ""
        if worker_name:
            self.worker_title.setText(tr("Worker log: {name}", self.language, name=worker_name))
        else:
            self.worker_title.setText(tr("Worker log: no worker selected", self.language))
        self._refresh_views()

    def _refresh_views(self) -> None:
        self.master_edit.setPlainText(self._filter_text(self._master_source))
        self.worker_edit.setPlainText(self._filter_text(self._worker_source))

    def _filter_text(self, text: str) -> str:
        level = str(self.level_combo.currentData() or "all")
        if level == "all":
            return text
        return "\n".join(line for line in text.splitlines() if f"[{level}]" in line.upper())


class ElidedValueLabel(QLabel):
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), Qt.AlignLeft | Qt.AlignVCenter, text)


class MetricCard(QFrame):
    def __init__(self, title: str, accent: str = "neutral", parent=None, *, icon_path=None) -> None:
        super().__init__(parent)
        self.setObjectName("metricCard")

        self.title_label = QLabel(title)
        self.title_label.setObjectName("metricCardTitle")
        self.title_label.setWordWrap(True)
        self.value_label = ElidedValueLabel("-")
        self.value_label.setObjectName("metricCardValue")
        self.value_label.setFixedHeight(22)
        self.value_label.setMinimumWidth(0)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        if icon_path and icon_path.is_file():
            icon = QLabel()
            icon.setPixmap(QPixmap(str(icon_path)))
            layout.addWidget(icon)
        text = QVBoxLayout()
        text.setSpacing(5)
        text.addWidget(self.title_label)
        text.addWidget(self.value_label)
        layout.addLayout(text, 1)
        self.set_accent(accent)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)
        self.value_label.setToolTip(value)

    def set_accent(self, accent: str) -> None:
        self.value_label.setStyleSheet(f"color: {ACCENT_COLORS.get(accent, ACCENT_COLORS['neutral'])};")


def worker_status_state(worker: dict) -> str:
    if not worker.get("enabled", True):
        return "disabled"

    status = str(worker.get("status") or "offline").lower()
    has_alert = (
        worker.get("error")
        or worker.get("blendAccessible") is False
        or worker.get("outputAccessible") is False
        or not worker.get("blenderFound", False)
    )

    if status == "rendering":
        return "rendering"
    if status in {"timeout", "stalled"}:
        return "timeout"
    if status == "offline":
        return "offline"
    if status == "error":
        return "error"
    if status == "idle" and has_alert:
        return "alert"
    if has_alert:
        return "alert"
    if status == "idle":
        return "idle"
    return status


def worker_status_view(worker: dict) -> tuple[str, str]:
    from renderfarm_shared.presentation import SESSION_STATUS_LABELS
    status = worker_status_state(worker)
    if status in SESSION_STATUS_LABELS:
        return SESSION_STATUS_LABELS[status], STATUS_COLORS["blue"] if status in {"session_open", "queued"} else STATUS_COLORS["orange"]
    if status == "rendering":
        return "Rendering", STATUS_COLORS["blue"]
    if status == "idle":
        return "Online", STATUS_COLORS["green"]
    if status == "timeout":
        return "Timeout", STATUS_COLORS["orange"]
    if status == "offline":
        return "Offline", STATUS_COLORS["red"]
    if status == "error":
        return "Error", STATUS_COLORS["red"]
    if status == "alert":
        return "Alert", STATUS_COLORS["yellow"]
    if status == "disabled":
        return "Disabled", STATUS_COLORS["gray"]
    return status.capitalize(), STATUS_COLORS["gray"]


def job_status_view(payload: dict, language: str = "en") -> tuple[str, str]:
    status = str(payload.get("status") or "queued").lower()
    label, color = JOB_STATUS_LABELS.get(status, (status.capitalize(), STATUS_COLORS["gray"]))
    return tr(label, language), color


def create_status_label(text: str, color: str) -> QLabel:
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setStyleSheet(
        f"""
        QLabel {{
            background: {color};
            color: #09121d;
            border: 1px solid rgba(255, 255, 255, 0.10);
            border-radius: 10px;
            padding: 4px 10px;
            font-weight: 700;
        }}
        """
    )
    return label


def build_small_button(text: str, callback) -> QPushButton:
    button = QPushButton(text)
    button.setFixedWidth(72)
    button.clicked.connect(callback)
    return button
