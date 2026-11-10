import ast
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from app.core.settings import AppSettings
from app.services.persistence import PersistenceService
from app.ui.main_window import MainWindow
from app.ui.widgets import LogViewerDialog, worker_status_view
from app.ui.theme import apply_app_theme
from renderfarm_shared.i18n import tr, PT, ES
from renderfarm_shared.presentation import relative_age, parse_timestamp, resource_path


@pytest.mark.parametrize("language,expected", [("en","2m ago"),("pt","há 2min"),("es","hace 2min")])
@pytest.mark.parametrize("kind", ["iso", "z", "unix", "numeric_string", "offset"])
def test_heartbeat_accepts_current_and_legacy_api_formats(language, expected, kind):
    now=datetime(2026,9,15,15,0,tzinfo=timezone.utc)
    timestamp=now-timedelta(seconds=125)
    values={"iso":timestamp.isoformat(), "z":timestamp.isoformat().replace("+00:00","Z"),
            "unix":timestamp.timestamp(), "numeric_string":str(timestamp.timestamp()),
            "offset":timestamp.astimezone(timezone(timedelta(hours=-3))).isoformat()}
    assert relative_age(values[kind],language,now=now)==expected


@pytest.mark.parametrize("value",[None,"", "bad date", "nan", float("inf"), -1e100, {}, True])
def test_bad_heartbeat_does_not_crash_or_invent_a_date(value):
    assert parse_timestamp(value) is None
    assert relative_age(value)=="-"


def test_future_heartbeat_is_clamped():
    now=datetime.now(timezone.utc)
    assert relative_age(now+timedelta(hours=1),"pt",now=now)=="Agora"


def test_standalone_loads_resources_from_bundle_and_config_stays_external(tmp_path, monkeypatch):
    bundle=tmp_path/'_MEI-test';bundle.mkdir()
    (bundle/'img').mkdir();(bundle/'img/brand.png').write_bytes(b'asset')
    runtime=tmp_path/'portable';runtime.mkdir()
    monkeypatch.setattr(sys,'_MEIPASS',str(bundle),raising=False)
    assert resource_path(runtime,'img','brand.png')==bundle/'img/brand.png'
    assert PersistenceService(runtime,runtime/'config.json').config_path==runtime/'config.json'


class StaticController:
    mode="coordinator"
    supports_local_jobs=False
    supports_local_job_files=False
    supports_worker_selection=False
    supports_worker_device_update=True
    supports_rescan=False
    def __init__(self,language):
        self.settings=AppSettings(mode="coordinator",language=language,master_name="QA")
    def get_snapshot(self):
        return {"workers":[],"jobs":[],"queue":[],"job":None,"metrics":{},"logs":{"general":"QA"}}
    tick=get_snapshot
    def update_job_options(self,**kwargs): pass


@pytest.fixture
def qa_window(tmp_path):
    app=QApplication.instance() or QApplication([])
    from PySide6.QtGui import QFontDatabase
    for font in ('segoeui.ttf', 'segoeuib.ttf'):
        path=Path('C:/Windows/Fonts')/font
        if path.is_file(): QFontDatabase.addApplicationFont(str(path))
    apply_app_theme(app)
    window=MainWindow(base_dir=Path(__file__).resolve().parents[1],job_manager=StaticController('en'),
                      persistence=PersistenceService(tmp_path,tmp_path/'config.json'),logger=Mock(),
                      default_block_size=10,default_worker_port=9000,poll_interval_ms=600000)
    window.poll_timer.stop()
    yield app,window
    window.close(); window.deleteLater(); app.processEvents()


@pytest.mark.parametrize("language",["en","pt","es"])
def test_small_window_keeps_controls_reachable_and_localized(qa_window,language):
    app,window=qa_window
    window._set_language(language,persist=False)
    window.resize(1040,600);window.show()
    for _ in range(4): app.processEvents()
    assert window.height()==600
    assert window.width()==1040
    assert window.select_output_button.width() >= window.select_output_button.sizeHint().width()
    assert window.workers_table.horizontalHeaderItem(4).text()==tr('Status',language)
    assert window.queue_table.horizontalHeaderItem(0).text()==tr('Job',language)
    window._rebuild_worker_table([{'id':'qa','name':'QA','status':'idle','enabled':True,'blenderFound':True}])
    assert window.workers_table.cellWidget(0,4).text()==tr('Online',language)
    window._rebuild_worker_table([{'id':'qa','name':'QA','status':'session_open','enabled':True,'blenderFound':True}])
    assert window.workers_table.cellWidget(0,4).text()==tr('Session ready',language)
    assert window._format_worker_frame_text({},None,None)==tr('Free',language)
    assert window.worker_status_filter.itemText(1)==tr('Rendering',language)
    area=window.content_scroll
    area.ensureWidgetVisible(window.settings_button)
    for _ in range(3): app.processEvents()
    point=window.settings_button.mapTo(area.viewport(),window.settings_button.rect().center())
    assert area.viewport().rect().contains(point)
    assert window.settings_button.isVisible()
    area.ensureWidgetVisible(window.start_button)
    for _ in range(3): app.processEvents()
    point=window.start_button.mapTo(area.viewport(),window.start_button.rect().center())
    assert area.viewport().rect().contains(point)
    assert not window._format_relative_age(datetime.now(timezone.utc).isoformat())=='-'


@pytest.mark.parametrize("language",["en","pt","es"])
def test_log_dialog_and_alert_status_have_translations(qa_window,language):
    app,window=qa_window
    dialog=LogViewerDialog(window,language=language)
    try:
        dialog.set_logs("raw diagnostic log","QA-WORKER","raw Blender log")
        assert dialog.worker_title.text()==tr("Worker log: {name}",language,name="QA-WORKER")
        assert dialog.level_combo.itemText(0)==tr("All levels",language)
        label,_=worker_status_view({"status":"idle","enabled":True,"error":"oops"})
        assert tr(label,language)==tr("Alert",language)
    finally: dialog.close();dialog.deleteLater();app.processEvents()


def test_every_literal_translation_key_in_desktop_ui_has_both_translations():
    root=Path(__file__).resolve().parents[3]
    files=[root/'master/blender_render_farm_master/app/ui/main_window.py',
           root/'coordinator/blender_render_farm_coordinator/app/gui/window.py',
           root/'worker/blender_render_farm_worker/app/device_selector.py',
           root/'image_viewer/BlenderImageViewer/app/ui/main_window.py',
           root/'master/blender_render_farm_master/app/ui/widgets.py',
           root/'shared/renderfarm_shared/presentation.py']
    missing=[]
    for file in files:
        for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
            status_catalogs={'STATUS_LABELS','STATUS_DISPLAY','JOB_STATUS_DISPLAY','JOB_STATUS_LABELS','SESSION_STATUS_LABELS'}
            if isinstance(node,ast.Assign) and isinstance(node.value,ast.Dict) and any(isinstance(target,ast.Name) and target.id in status_catalogs for target in node.targets):
                for value in node.value.values:
                    value=value.elts[0] if isinstance(value,ast.Tuple) else value
                    if isinstance(value,ast.Constant) and isinstance(value.value,str):
                        if value.value not in PT or value.value not in ES: missing.append((file.name,node.lineno,value.value))
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='_t' and node.args:
                if isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str):
                    key=node.args[0].value
                    if key not in PT or key not in ES: missing.append((file.name,node.lineno,key))
    assert not missing,missing


@pytest.mark.parametrize('language,expected',[('en','The .blend file does not exist.'),('pt','O arquivo .blend não existe.'),('es','El archivo .blend no existe.')])
def test_old_service_validation_messages_use_the_ui_language(language,expected):
    from renderfarm_shared.i18n import translate_message
    assert translate_message('O arquivo .blend nao existe.',language)==expected
    url='http://127.0.0.1:18010'
    result=translate_message(f'Conectado ao Coordinator em {url}.',language)
    assert url in result
    assert result.startswith({'en':'Connected','pt':'Conectado','es':'Conectado'}[language])
    assert translate_message('driver diagnostic 123',language)=='driver diagnostic 123'
