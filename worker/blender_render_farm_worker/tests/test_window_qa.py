import json
from pathlib import Path
import tkinter as tk
from unittest.mock import Mock, patch
import pytest
from app.config import ConfigManager
from app.status_manager import WorkerStatusManager
from app.device_selector import RenderDeviceWindow
from renderfarm_shared.i18n import tr


@pytest.fixture(scope='module')
def tk_app():
    root=tk.Tk()
    yield root
    root.destroy()


@pytest.fixture
def gui_root(tk_app):
    # A real application keeps one Tcl interpreter while rebuilding its UI.
    yield tk_app
    for callback in tk_app.tk.call('after','info'):
        tk_app.after_cancel(callback)
    for child in tk_app.winfo_children():
        child.destroy()
    tk_app.update_idletasks()


@pytest.mark.parametrize('language',['en','pt','es'])
@pytest.mark.parametrize('scale',[1.0,1.25,1.5])
def test_worker_small_window_and_connection_copy(tmp_path,language,scale,gui_root):
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'language':language,'workerName':'QA-WORKER','coordinatorDiscoveryEnabled':False}))
    manager=ConfigManager(config)
    status=WorkerStatusManager(manager.config)
    window=RenderDeviceWindow(config_manager=manager,status_manager=status,logger=Mock())
    root=gui_root;window._root=root;window._tk=tk
    original_scale=root.tk.call('tk','scaling')
    try:
        root.tk.call('tk','scaling',float(original_scale)*scale)
        window._configure_geometry(root)
        width,height=int(1366/scale),int(680/scale)
        root.geometry(f'{width}x{height}')
        window._build_window()
        window._sync_from_config()
        root.update()
        assert root.winfo_height()==height
        assert root.winfo_width()==width
        window._test_connection()
        assert window._ui['test_detail'].cget('text')==tr('Coordinator is not connected yet.',language)
        assert window._build_api_detail({'coordinatorDiscoveryEnabled':False})==tr('Coordinator discovery is disabled',language)+' | '+tr('Blender: waiting for detection',language)
        window.content_scroll.canvas.yview_moveto(1)
        root.update()
        assert window.content_scroll.canvas.yview()[1] == 1
        window._apply_selection('CPU')
        assert status.status_snapshot()['renderDevicePreference']=='CPU'
        window._apply_selection('GPU')
        assert status.status_snapshot()['renderDevicePreference']=='GPU'
        if scale==1.0:
            root.geometry('1620x940')
            snapshot=status.status_snapshot()
            snapshot.update(blenderFound=True,blenderVersion='5.1.2',gpu='NVIDIA GeForce RTX 3070 Laptop GPU',coordinatorConnected=True,coordinatorApiUrl='http://127.0.0.1:18010')
            with patch.object(status,'status_snapshot',return_value=snapshot):
                window._refresh_status()
                root.update()
            assert window.content_scroll.canvas.yview()==(0.0,1.0)
    finally:
        root.tk.call('tk','scaling',original_scale)


def test_connection_probe_failure_is_returned_to_ui_queue(tmp_path):
    manager=ConfigManager(tmp_path/'config.json')
    window=RenderDeviceWindow(config_manager=manager,status_manager=WorkerStatusManager(manager.config),logger=Mock())
    with patch('app.device_selector.urllib.request.urlopen',side_effect=OSError('connection refused')):
        window._probe_connection('http://127.0.0.1:1')
    success,message=window._connection_results.get_nowait()
    assert not success
    assert 'connection refused' in message


@pytest.mark.parametrize('language',['en','pt','es'])
@pytest.mark.parametrize('state',['session_open','queued','draining','closing'])
def test_persistent_session_copy_does_not_claim_missing_frames(tmp_path,language,state):
    from app.device_selector import humanize_status
    from renderfarm_shared.presentation import SESSION_STATUS_LABELS
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'language':language}))
    manager=ConfigManager(config)
    window=RenderDeviceWindow(config_manager=manager,status_manager=WorkerStatusManager(manager.config),logger=Mock())
    label=tr(SESSION_STATUS_LABELS[state],language)
    assert humanize_status(state,language)==label
    text=window._build_worker_detail({'status':state,'currentJob':'QA','currentFrame':None,'renderDevicePreference':'GPU'})
    assert label in text and 'None' not in text
    assert tr('Rendering',language) not in text


def test_scrollable_frame_refreshes_after_dynamic_text_grows(gui_root):
    from renderfarm_shared.tk_layout import ScrollableFrame
    root=gui_root
    try:
        root.geometry('800x400')
        area=ScrollableFrame(root,background='#061526',min_width=400)
        area.pack(fill='both',expand=True)
        label=tk.Label(area.content,text='Short GPU name')
        label.pack()
        footer=tk.Label(area.content,text='Footer must remain reachable')
        footer.pack()
        root.update()
        assert area.canvas.yview()==(0.0,1.0)
        label.configure(text='Long hardware details\n'*40)
        area.refresh();root.update()
        assert area.vertical.winfo_ismapped()
        assert area.canvas.yview()[1]<1.0
        area.canvas.yview_moveto(1);root.update()
        bottom=footer.winfo_rooty()+footer.winfo_height()
        assert bottom<=area.canvas.winfo_rooty()+area.canvas.winfo_height()
    finally:
        area.destroy()
