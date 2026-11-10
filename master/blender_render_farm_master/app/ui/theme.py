from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


APP_STYLE_SHEET = """
QWidget {
    background: #061426;
    color: #e6edf6;
    font-family: "Segoe UI";
    font-size: 13px;
}

QLabel,
QCheckBox {
    background: transparent;
}

QMainWindow, QWidget#centralRoot {
    background: #031226;
}

QGroupBox {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #0a213a,
        stop: 1 #06182c
    );
    border: 1px solid #0a5f9f;
    border-radius: 8px;
    margin-top: 12px;
    padding: 10px 10px 8px 10px;
    font-weight: 700;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    top: -1px;
    padding: 0 6px;
    color: #00b8ff;
    background: transparent;
}

QFrame#actionStrip,
QFrame#footerBar,
QFrame#infoStrip,
QFrame#logToolbar,
QFrame#queueFooter {
    background: #071b31;
    border: 1px solid #0a5f9f;
    border-radius: 10px;
}

QFrame#footerBar {
    border-radius: 8px;
}

QFrame#metricCard {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #0b2743,
        stop: 1 #0a213a
    );
    border: 1px solid #125d91;
    border-radius: 7px;
}

QLabel#metricCardTitle {
    color: #8fb9e8;
    font-size: 11px;
    font-weight: 600;
}

QLabel#metricCardValue {
    color: #eef4fb;
    font-size: 16px;
    font-weight: 700;
}

QLabel#captionLabel {
    color: #92a0b4;
    font-size: 11px;
    font-weight: 600;
}

QLabel#infoValue,
QLabel#mutedLabel {
    color: #dfe8f5;
}

QLabel#infoValue {
    font-size: 14px;
    font-weight: 700;
}

QLabel#footerText {
    color: #aeb9ca;
}

QFrame#footerDivider {
    background: #273244;
    min-width: 1px;
    max-width: 1px;
}

QLineEdit,
QSpinBox,
QComboBox,
QPlainTextEdit {
    background: #06182b;
    border: 1px solid #145987;
    border-radius: 7px;
    padding: 5px 9px;
    selection-background-color: #2e5375;
}

QLineEdit:focus,
QSpinBox:focus,
QComboBox:focus,
QPlainTextEdit:focus {
    border: 1px solid #20a9ff;
}

QLineEdit:disabled,
QSpinBox:disabled,
QComboBox:disabled,
QPlainTextEdit:disabled {
    color: #7d8696;
    background: #121821;
    border-color: #222c3c;
}

QComboBox::drop-down,
QSpinBox::up-button,
QSpinBox::down-button {
    border: none;
    width: 22px;
    background: transparent;
}

QPushButton {
    background: #0b2743;
    color: #eef4fb;
    border: 1px solid #125d91;
    border-radius: 8px;
    padding: 0 12px;
    min-height: 30px;
    font-weight: 700;
}

QPushButton:hover {
    background: #113354;
    border-color: #3a4a60;
}

QPushButton:pressed {
    background: #071a2d;
}

QPushButton:disabled {
    color: #6f7988;
    background: #141b25;
    border-color: #222c3b;
}

QPushButton[variant="primary"] {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #ff8c25,
        stop: 1 #ffad4c
    );
    color: #ffffff;
    border-color: #ffb75c;
}

QPushButton[variant="primary"]:hover {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #ff9837,
        stop: 1 #ffb95c
    );
}

QPushButton[variant="info"] {
    color: #3eb7ff;
}

QPushButton[variant="danger"] {
    color: #ff6969;
}

QPushButton[variant="footer"] {
    min-height: 30px;
    padding: 0 16px;
}

QTableWidget,
QTabWidget::pane {
    background: #071c31;
    border: 1px solid #0a5f9f;
    border-radius: 8px;
}

QTableWidget {
    gridline-color: #103f64;
    alternate-background-color: #092139;
    selection-background-color: #124875;
    selection-color: #ffffff;
}

QHeaderView::section {
    background: #0b2945;
    color: #bddcff;
    border: none;
    border-right: 1px solid #145987;
    border-bottom: 1px solid #145987;
    padding: 6px 9px;
    font-weight: 700;
}

QTableCornerButton::section {
    background: #0b2945;
    border: none;
    border-right: 1px solid #145987;
    border-bottom: 1px solid #145987;
}

QProgressBar {
    min-height: 14px;
    border: 1px solid #125d91;
    border-radius: 7px;
    background: #06182b;
    color: #eef4fb;
    text-align: center;
    font-weight: 700;
}

QProgressBar::chunk {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #087cff,
        stop: 1 #20c3ff
    );
    border-radius: 6px;
}

QTabBar::tab {
    background: #071c31;
    color: #aeb9ca;
    border: 1px solid #0a5f9f;
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 8px 14px;
    margin-right: 4px;
}

QTabBar::tab:selected {
    background: #0b2945;
    color: #eef4fb;
}

QScrollBar:vertical {
    background: #0e141c;
    width: 12px;
    margin: 2px;
}

QScrollBar::handle:vertical {
    background: #166394;
    border-radius: 6px;
    min-height: 24px;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical,
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {
    background: transparent;
    border: none;
}

QScrollBar:horizontal {
    background: #0e141c;
    height: 12px;
    margin: 2px;
}

QScrollBar::handle:horizontal {
    background: #166394;
    border-radius: 6px;
    min-width: 24px;
}

QCheckBox {
    spacing: 8px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 5px;
    border: 1px solid #314054;
    background: #06182b;
}

QCheckBox::indicator:checked {
    background: #ff972f;
    border-color: #ffae55;
}

QSplitter::handle {
    background: #0b1017;
    margin: 0 4px;
}

QMessageBox,
QDialog {
    background: #06182c;
}
"""


def apply_app_theme(app: QApplication) -> None:
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#031226"))
    palette.setColor(QPalette.WindowText, QColor("#e6edf6"))
    palette.setColor(QPalette.Base, QColor("#06182b"))
    palette.setColor(QPalette.AlternateBase, QColor("#141d28"))
    palette.setColor(QPalette.Text, QColor("#e6edf6"))
    palette.setColor(QPalette.Button, QColor("#0b2743"))
    palette.setColor(QPalette.ButtonText, QColor("#eef4fb"))
    palette.setColor(QPalette.Highlight, QColor("#2f8fd4"))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipBase, QColor("#1b2432"))
    palette.setColor(QPalette.ToolTipText, QColor("#eef4fb"))

    app.setPalette(palette)
    app.setStyleSheet(APP_STYLE_SHEET)
