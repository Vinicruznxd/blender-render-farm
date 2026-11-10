import os
from unittest.mock import Mock, patch
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor

from app.core.settings import ViewerSettings
from app.services.persistence import PersistenceService
from app.ui.main_window import MainWindow


def test_language_change_preserves_review_and_coordinator_state(tmp_path):
    app = QApplication.instance() or QApplication([])
    folder = tmp_path / "renders"
    folder.mkdir()
    for frame in (1, 2):
        image = QImage(16, 16, QImage.Format_RGB32)
        image.fill(QColor("blue"))
        image.save(str(folder / f"frame_{frame:04}.png"))
    settings = ViewerSettings(last_image_folder=str(folder))
    with patch("app.ui.main_window.QTimer.singleShot"):
        window = MainWindow(base_dir=tmp_path, settings=settings,
                            persistence=PersistenceService(tmp_path, tmp_path / "config.json"), logger=Mock())
    try:
        window._select_all_renderable()
        window.current_index = 1
        selected = set(window.selected_paths)
        window.detected_coordinator_url = "http://127.0.0.1:18010"
        window.coordinator_readout.setText("QA | http://127.0.0.1:18010")
        window.language_combo.setCurrentIndex(window.language_combo.findData("es"))
        assert window.current_index == 1
        assert window.selected_paths == selected
        assert window.position_label.text() == "2 / 2"
        assert window.coordinator_readout.text() == "QA | http://127.0.0.1:18010"
        assert "2 imagen" in window.status_message_label.text()
        window.detected_coordinator_url = None
        window.language_combo.setCurrentIndex(window.language_combo.findData("pt"))
        assert "Detectando" not in window.coordinator_readout.text()
        assert "Nenhum Coordinator" in window.coordinator_readout.text()
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('language',['en','pt','es'])
def test_small_viewer_keeps_rerender_and_source_controls_reachable(tmp_path,language):
    from renderfarm_shared.i18n import tr
    app=QApplication.instance() or QApplication([])
    with patch('app.ui.main_window.QTimer.singleShot'):
        window=MainWindow(base_dir=tmp_path, settings=ViewerSettings(language=language),
                          persistence=PersistenceService(tmp_path,tmp_path/'config.json'),logger=Mock())
    try:
        window.resize(1000,600);window.show()
        for _ in range(4): app.processEvents()
        assert window.height()==600
        area=window.content_scroll
        area.ensureWidgetVisible(window.render_button)
        for _ in range(3): app.processEvents()
        assert area.viewport().rect().contains(window.render_button.mapTo(area.viewport(),window.render_button.rect().center()))
        sidebar=window.sidebar_scroll
        sidebar.ensureWidgetVisible(window.open_dashboard_button)
        for _ in range(3): app.processEvents()
        assert sidebar.viewport().rect().contains(window.open_dashboard_button.mapTo(sidebar.viewport(),window.open_dashboard_button.rect().center()))
        assert window.timeline_hint_label.text()==tr('Mouse wheel or arrows',language)
        from app.core.frame_selection import ImageEntry
        window.entries=[ImageEntry(path=tmp_path/'preview.png',frame_number=None)]
        window.current_index=0
        window._update_timeline_controls()
        assert window.timeline_hint_label.text()==tr('No frame',language)
    finally:
        window.close();window.deleteLater();app.processEvents()
