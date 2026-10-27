import pytest


@pytest.fixture(autouse=True)
def isolate_monitor_and_scheduler_health_writes(request, tmp_path, monkeypatch):
    """These integration tests must never publish health into a running Bot."""
    name = request.path.name
    if not (name.startswith("test_etf_monitor") or name in {
        "test_scheduled_task_queue.py", "test_scheduled_task_service.py",
    }):
        return
    from stock_ai_bot.monitoring import bot_runtime_health as health

    for name in ("update_schedule_health", "record_scheduled_status_heartbeat_event"):
        function = getattr(health, name)
        # Change only keyword-only defaults in the test process, including
        # references already imported by main.py or a service constructor.
        monkeypatch.setattr(function, "__kwdefaults__", {
            **function.__kwdefaults__, "path": tmp_path / "bot_heartbeat.json",
        })

    from research_center.scheduled_task_service import write_scheduled_task_audit_event

    monkeypatch.setattr(write_scheduled_task_audit_event, "__kwdefaults__", {
        **write_scheduled_task_audit_event.__kwdefaults__,
        "audit_dir": tmp_path / "scheduled-audit",
    })
