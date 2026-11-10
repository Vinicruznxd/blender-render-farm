"""A completed job must not reserve an idle Blender session until its timeout."""
import pytest
from test_coordinator import _register_worker, _create_job


@pytest.mark.parametrize('terminal_status',['completed','cancelled','error'])
@pytest.mark.parametrize('fail_once',[False,True])
def test_missed_terminal_drain_recovers_after_poll(coordinator_app,monkeypatch,terminal_status,fail_once):
    app,client,fake,tmp_path=coordinator_app
    url='http://127.0.0.1:9000'
    fake.add_worker(url,worker_id='worker_a',name='A',persistent_session_enabled=True)
    registered=_register_worker(client)
    previous=_create_job(client,tmp_path/'previous',name='Previous',start_frame=1,end_frame=1)
    app.state.database.execute('UPDATE jobs SET status = ? WHERE jobId = ?',(terminal_status,previous['jobId']))
    app.state.database.execute('UPDATE job_frames SET status = ? WHERE jobId = ?',('done' if terminal_status=='completed' else 'cancelled',previous['jobId']))
    fake.workers[url].update(status='session_open',sessionState='session_open',currentJobId=previous['jobId'],currentFrame=None)
    # Reproduce completion clearing the coordinator assignment before notification.
    app.state.worker_registry.clear_worker_assignment(registered['workerId'],status='idle')
    app.state.service._notify_workers_job_drained(previous['jobId'])
    assert fake.job_drain_requests==[]
    next_job=_create_job(client,tmp_path/'next',name='Next',start_frame=1,end_frame=1)
    original=fake.job_drain
    calls=[]
    def drain(base_url,payload):
        calls.append(payload)
        if fail_once and len(calls)==1:
            raise OSError('transient connection loss')
        return original(base_url,payload)
    monkeypatch.setattr(fake,'job_drain',drain)
    for _ in range(3):
        app.state.service.run_scheduler_cycle()
    assert fake.job_drain_requests[0]['jobId']==previous['jobId']
    assert any(item['jobId']==next_job['jobId'] for item in fake.render_requests)
    assert fake.workers[url]['currentJobId']==next_job['jobId']


@pytest.mark.parametrize('active_status',['running','paused','blocked'])
def test_terminal_recovery_does_not_drain_unfinished_jobs(coordinator_app,active_status):
    app,client,fake,tmp_path=coordinator_app
    url='http://127.0.0.1:9000'
    fake.add_worker(url,worker_id='worker_a',name='A',persistent_session_enabled=True)
    _register_worker(client)
    job=_create_job(client,tmp_path/'active',name='Active',start_frame=1,end_frame=2)
    app.state.database.execute('UPDATE jobs SET status = ? WHERE jobId = ?',(active_status,job['jobId']))
    fake.workers[url].update(status='session_open',sessionState='session_open',currentJobId=job['jobId'])
    app.state.worker_registry.refresh_workers()
    app.state.service._drain_finished_job_sessions()
    assert fake.job_drain_requests==[]
