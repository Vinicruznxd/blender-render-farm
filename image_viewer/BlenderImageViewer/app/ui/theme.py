from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


APP_STYLE_SHEET = """
QWidget {
    background: #0f141d;
    color: #e6edf6;
    font-family: "Segoe UI";
    font-size: 13px;
}

QLabel,
QCheckBox {
    background: transparent;
}

QMainWindow, QWidget#centralRoot {
    background: #0d1219;
}

QGroupBox {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #161d28,
        stop: 1 #111720
    );
    border: 1px solid #263244;
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
    color: #ff972f;
    background: transparent;
}

QFrame#actionStrip,
QFrame#footerBar,
QFrame#infoStrip,
QFrame#viewerShell,
QFrame#sidePanel,
QFrame#viewerCard,
QFrame#imageStage {
    background: #121923;
    border: 1px solid #263244;
    border-radius: 10px;
}

QFrame#metricCard,
QFrame#thumbnailCard,
QFrame#galleryCard,
QFrame#emptyStateCard {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #1a2230,
        stop: 1 #161d28
    );
    border: 1px solid #2b3748;
    border-radius: 9px;
}

QFrame#thumbnailCard[empty="true"] {
    border-style: dashed;
}

QFrame#galleryCard[selected="true"] {
    border: 1px solid #ffb75c;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #1f2938,
        stop: 1 #182232
    );
}

QLabel#heroTitle {
    color: #eef4fb;
    font-size: 24px;
    font-weight: 700;
}

QLabel#heroSubtitle {
    color: #aebad0;
    font-size: 13px;
}

QLabel#captionLabel {
    color: #92a0b4;
    font-size: 11px;
    font-weight: 600;
}

QLabel#pathValue,
QLabel#statusValue,
QLabel#viewerMeta,
QLabel#infoValue,
QLabel#mutedLabel {
    color: #dfe8f5;
}

QLabel#pathValue,
QLabel#statusValue,
QLabel#infoValue {
    font-size: 14px;
    font-weight: 700;
}

QLabel#viewerMeta,
QLabel#mutedLabel {
    color: #aeb9ca;
}

QLabel#viewerCardTitle {
    color: #eef4fb;
    font-size: 14px;
    font-weight: 700;
}

QLabel#viewerImage {
    background: #0f1620;
    border: 1px solid #2a3546;
    border-radius: 8px;
    padding: 8px;
}

QLabel#heroImage {
    background: #0f1620;
    border: 1px solid #2a3546;
    border-radius: 18px;
    padding: 0;
}

QLabel#galleryImage {
    background: #0f1620;
    border: 1px solid #223041;
    border-radius: 12px;
    padding: 0;
}

QLabel#emptyStateTitle {
    color: #eef4fb;
    font-size: 22px;
    font-weight: 700;
}

QLabel#emptyStateSubtitle {
    color: #8fa5c4;
    font-size: 14px;
}

QLineEdit,
QComboBox,
QPlainTextEdit {
    background: #0f1620;
    border: 1px solid #2a3546;
    border-radius: 7px;
    padding: 5px 9px;
    selection-background-color: #2e5375;
}

QLineEdit:focus,
QComboBox:focus,
QPlainTextEdit:focus {
    border: 1px solid #37a9ff;
}

QComboBox::drop-down {
    border: none;
    width: 22px;
    background: transparent;
}

QLineEdit:read-only {
    color: #9ab1ce;
    background: #101723;
}

QPushButton {
    background: #1a2230;
    color: #eef4fb;
    border: 1px solid #2b3748;
    border-radius: 8px;
    padding: 0 12px;
    min-height: 30px;
    font-weight: 700;
}

QPushButton:hover {
    background: #202a39;
    border-color: #3a4a60;
}

QPushButton:pressed {
    background: #141c26;
}

QPushButton:disabled, QPushButton[variant="primary"]:disabled, QPushButton[variant="info"]:disabled {
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

QPushButton#galleryToggle {
    min-width: 24px;
    max-width: 24px;
    min-height: 24px;
    max-height: 24px;
    padding: 0;
    border-radius: 12px;
    background: rgba(6, 16, 28, 0.84);
    border: 1px solid #314054;
    color: transparent;
    font-size: 16px;
    font-weight: 700;
}

QPushButton#galleryToggle:checked {
    background: #ff972f;
    border-color: #ffb75c;
    color: #ffffff;
}

QListWidget {
    background: transparent;
    border: none;
    outline: none;
}

QListWidget::item {
    background: transparent;
    border: none;
    padding: 0;
}

QListWidget::item:selected {
    background: transparent;
    color: #eef4fb;
}

QSlider::groove:horizontal {
    height: 6px;
    border-radius: 3px;
    background: #1d2734;
}

QSlider::sub-page:horizontal {
    border-radius: 3px;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #ff8c25,
        stop: 1 #ffad4c
    );
}

QSlider::handle:horizontal {
    width: 18px;
    height: 18px;
    margin: -6px 0;
    border-radius: 9px;
    background: #eef4fb;
    border: 1px solid #ffb75c;
}

QProgressBar {
    min-height: 14px;
    border: 1px solid #2b3748;
    border-radius: 7px;
    background: #0f1620;
    color: #eef4fb;
    text-align: center;
    font-weight: 700;
}

QTabWidget::pane {
    background: #131b25;
    border: 1px solid #263244;
    border-radius: 8px;
}

QScrollBar:vertical {
    background: #0e141c;
    width: 12px;
    margin: 2px;
}

QScrollBar::handle:vertical {
    background: #2e3b4d;
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
    background: #2e3b4d;
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
    background: #0f1620;
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
    background: #111720;
}
"""


def apply_app_theme(app: QApplication) -> None:
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#0d1219"))
    palette.setColor(QPalette.WindowText, QColor("#e6edf6"))
    palette.setColor(QPalette.Base, QColor("#0f1620"))
    palette.setColor(QPalette.AlternateBase, QColor("#141d28"))
    palette.setColor(QPalette.Text, QColor("#e6edf6"))
    palette.setColor(QPalette.Button, QColor("#1a2230"))
    palette.setColor(QPalette.ButtonText, QColor("#eef4fb"))
    palette.setColor(QPalette.Highlight, QColor("#2f8fd4"))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipBase, QColor("#1b2432"))
    palette.setColor(QPalette.ToolTipText, QColor("#eef4fb"))

    app.setPalette(palette)
    app.setStyleSheet(APP_STYLE_SHEET)
