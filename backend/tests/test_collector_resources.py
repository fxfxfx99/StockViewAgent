import subprocess

from app.services import collector_resources as resources


def test_current_memory_can_resume_after_pressure_and_does_not_use_peak(monkeypatch):
    monkeypatch.setattr(resources, "_sample", (0.0, None))
    monkeypatch.setattr(resources, "_rss_bytes", lambda: 900 * 1024**2)
    assert resources.snapshot(768, fresh=True)["pressure"] is True
    monkeypatch.setattr(resources, "_rss_bytes", lambda: 300 * 1024**2)
    result = resources.snapshot(768, fresh=True)
    assert result["rss_mb"] == 300 and result["pressure"] is False
    assert result["scope"] == "current_process"


def test_unsupported_probe_is_explicit_and_does_not_disable_collection(monkeypatch):
    monkeypatch.setattr(resources, "_sample", (0.0, None))
    def unavailable():
        raise subprocess.TimeoutExpired("ps", 1)
    monkeypatch.setattr(resources, "_rss_bytes", unavailable)
    result = resources.snapshot(768, fresh=True)
    assert result["rss_mb"] is None and result["supported"] is False and result["pressure"] is False
