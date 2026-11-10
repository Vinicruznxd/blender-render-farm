from copy import deepcopy
import pytest

from app.gui.runtime import CoordinatorServerRuntime
from app.gui.window import CoordinatorWindow


@pytest.fixture(scope='module')
def tk_app():
    import tkinter as tk
    root=tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def coordinator_gui(tk_app,monkeypatch):
    import tkinter as tk
    monkeypatch.setattr('app.gui.window.tk.Tk',lambda:tk.Toplevel(tk_app))
    yield
    for callback in tk_app.tk.call('after','info'):
        tk_app.after_cancel(callback)
    for child in tk_app.winfo_children():
        child.destroy()
    tk_app.update_idletasks()


def _notebook(window):
    pending = [window.root]
    while pending:
        widget = pending.pop()
        if widget.winfo_class() == "TNotebook":
            return widget
        pending.extend(widget.winfo_children())
    raise AssertionError("Dashboard tabs missing")


def test_poll_uses_the_worker_machine_name(coordinator_app):
    app, _, _, _ = coordinator_app
    worker = app.state.worker_registry._extract_worker_state(
        "127.0.0.1", 9000, {}, {"machineName": "ARTIST-02", "status": "idle"}, {}
    )
    assert worker["name"] == "ARTIST-02"


def test_poll_reflects_cpu_gpu_changes_made_on_worker(coordinator_app):
    app, client, fake_workers, _ = coordinator_app
    url = "http://127.0.0.1:9000"
    fake_workers.add_worker(url, worker_id="device_qa", name="DEVICE-QA")
    client.post("/workers", json={"name": "DEVICE-QA", "ip": "127.0.0.1", "port": 9000,
                                  "renderDevicePreference": "GPU"})
    for preference in ("CPU", "GPU"):
        fake_workers.workers[url]["renderDevicePreference"] = preference
        fake_workers.workers[url]["effectiveUseGpu"] = preference == "GPU"
        workers = app.state.worker_registry.refresh_workers()
        assert workers[0]["renderDevicePreference"] == preference
        assert bool(workers[0]["effectiveUseGpu"]) == (preference == "GPU")


def test_new_unreachable_worker_stays_visible_for_diagnosis(coordinator_app):
    app, client, _, _ = coordinator_app
    app.state.worker_registry.register_or_update_worker({
        "workerId": "never_seen", "name": "NEW-WORKER", "ip": "127.0.0.12",
        "port": 9000, "status": "offline", "error": "Connection refused",
    })
    state = client.get("/state").json()
    assert any(worker["workerId"] == "never_seen" for worker in state["workers"])
    assert state["metrics"]["machinesOnline"] == 0


def test_language_change_preserves_logs_selected_tab_and_search(coordinator_app,coordinator_gui):
    app, _, _, _ = coordinator_app
    window = CoordinatorWindow(app, CoordinatorServerRuntime(app))
    window.root.withdraw()
    try:
        _notebook(window).select(1)
        window.worker_search_var.set("ARTIST")
        logs = {"general": "QA log line must remain visible"}
        state = deepcopy(window._latest_state)
        health = window._latest_health
        window._apply_dashboard_state(state, health, logs)
        state["coordinator"]["language"] = "es"
        window._apply_dashboard_state(state, health, logs)
        notebook = _notebook(window)
        assert "QA log line" in window.logs_text.get("1.0", "end")
        assert notebook.index(notebook.select()) == 1
        assert window.worker_search_var.get() == "ARTIST"
        assert "QA log line" in window.logs_text.get("1.0", "end")
        assert "QA log line" in window.recent_logs_text.get("1.0", "end")
    finally:
        if window._after_id:
            window.root.after_cancel(window._after_id)
        window.root.destroy()


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "http://127.0.0.1:8010"),
    ("192.168.0.167", "http://192.168.0.167:8010"),
    ("localhost", "http://localhost:8010"),
    ("::1", "http://[::1]:8010"),
])
def test_explicit_bind_advertises_only_the_listening_host(coordinator_app,host,expected):
    app,client,_,_=coordinator_app
    app.state.settings.host=host
    assert app.state.coordinator_discovery.snapshot()["apiUrls"]==[expected]
    assert client.get('/health').json()['discovery']['apiUrls']==[expected]


def test_wildcard_bind_keeps_lan_and_local_access(coordinator_app,monkeypatch):
    app,_,_,_=coordinator_app
    app.state.settings.host='0.0.0.0'
    discovery=app.state.coordinator_discovery
    monkeypatch.setattr(discovery,'_local_ipv4_addresses',lambda:['192.168.0.167','127.0.0.1'])
    assert discovery.snapshot()['apiUrls'][:2]==['http://192.168.0.167:8010','http://127.0.0.1:8010']


@pytest.mark.parametrize('language',['en','pt','es'])
@pytest.mark.parametrize('scale',[1.0,1.25,1.5])
def test_translated_status_filter_survives_language_change_and_small_window(coordinator_app,language,scale,coordinator_gui):
    from renderfarm_shared.i18n import tr
    app,_,_,_=coordinator_app
    app.state.settings.language=language
    window=CoordinatorWindow(app,CoordinatorServerRuntime(app))
    original_scale=window.root.tk.call('tk','scaling')
    try:
        window.root.tk.call('tk','scaling',float(original_scale)*scale)
        width,height=int(1366/scale),int(680/scale)
        window.root.geometry(f'{width}x{height}')
        window.root.update()
        assert window.root.winfo_width()==width
        assert window.root.winfo_height()==height
        assert window.worker_status_filter_var.get()==tr('All statuses',language)
        assert 'strict_exact' in window._format_job_blender({'versionRoutingPolicy': 'strict_exact'})
        assert window._format_job_frames({'totalFrames':1200,'frameStats':{'done':512,'rendering':4}})=='512 / 1200'
        window.worker_status_filter_var.set(tr('Rendering',language))
        assert window._filter_workers([{'status':'idle'}, {'status':'rendering'}])==[{'status':'rendering'}]
        window._rebuild_for_language('pt' if language!='pt' else 'es')
        assert window._filter_workers([{'status':'rendering'}])==[{'status':'rendering'}]
        window.root.update()
        assert window.content_scroll.canvas.yview()[1] < 1
        window.content_scroll.canvas.yview_moveto(1)
        assert window.content_scroll.canvas.yview()[1] == 1
    finally:
        if window._after_id: window.root.after_cancel(window._after_id)
        window.root.tk.call('tk','scaling',original_scale)
        window.root.destroy()
