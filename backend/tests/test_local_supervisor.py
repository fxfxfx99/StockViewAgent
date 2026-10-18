"""Exercise real child processes, local sockets, locks and signal cleanup."""
from __future__ import annotations

import importlib.util
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("sva_local_supervisor", ROOT / "scripts/local_supervisor.py")
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


@pytest.fixture(autouse=True)
def isolated_launcher_environment(monkeypatch):
    monkeypatch.delenv("STOCKVIEW_DATA_DIR", raising=False)

SERVICE_CODE = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, os, signal, subprocess, sys, threading, time
from pathlib import Path
root, name, port, crashes, spawn_child, ignore_term, unhealthy, broken_deps = sys.argv[1:]
root = Path(root)
port, crashes = int(port), int(crashes)
count_file = root / (name + '.count')
count = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(count))
(root / (name + '.events')).open('a').write(json.dumps({'pid': os.getpid(), 'attempt': count, 'at': time.monotonic()}) + '\n')
if ignore_term == '1':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if spawn_child == '1':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    (root / (name + '.child')).write_text(str(child.pid))
if port:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with (root / (name + '.health')).open('a') as stream: stream.write(str(time.monotonic()) + '\n')
            self.send_response(503 if unhealthy == '1' else 200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
print('fixture service running', flush=True)
if count <= crashes:
    time.sleep(.3)
    if broken_deps == '1':
        (root / 'dependencies.ready').unlink(missing_ok=True)
    raise SystemExit(23)
while True: time.sleep(.1)
'''

DRIVER_CODE = r'''
import importlib.util, json, sys
from pathlib import Path
module_path, root_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location('local_supervisor_test_driver', module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
root = Path(root_path)
plan = json.loads((root / 'plan.json').read_text())
config = module.Config(root=root, backend_port=plan['backend_port'], frontend_port=plan['frontend_port'],
    open_browser=plan.get('open_browser', False), restart_limit=plan.get('restart_limit', 2),
    restart_base_delay=.1, restart_max_delay=.2, stable_seconds=5,
    ready_timeout=plan.get('ready_timeout', 2), stop_timeout=.3, poll_interval=.05,
    health_interval=plan.get('health_interval', .15), startup_health_interval=.03,
    state_interval=plan.get('state_interval', .1), startup_state_interval=.05)
services = []
for name, port in [('backend', config.backend_port), ('frontend', config.frontend_port), ('research', 0)]:
    args = plan.get(name, {})
    command = [sys.executable, '-u', str(root / 'service.py'), str(root), name, str(port),
               str(args.get('crashes', 0)), str(int(args.get('spawn_child', False))),
               str(int(args.get('ignore_term', False))), str(int(args.get('unhealthy', False))),
               str(int(args.get('broken_deps', False)))]
    services.append(module.Service(name, command, root, f'http://127.0.0.1:{port}/' if port else None))
def browser(url):
    with (root / 'browser.events').open('a') as stream: stream.write(url + '\n')
runner = module.Supervisor(config, services=services,
    setup_command=[sys.executable, str(root / 'setup_fixture.py'), str(root)],
    dependency_check=lambda: (root / 'dependencies.ready').exists(), browser=browser)
raise SystemExit(runner.run())
'''


def available_ports():
    listeners = [socket.socket(), socket.socket()]
    try:
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in listeners]
    finally:
        for listener in listeners:
            listener.close()


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def wait_for(condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError("Condition not reached within test deadline")


def process_running(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@pytest.fixture
def runtime(tmp_path):
    processes = []
    def launch(plan=None, *, reuse=False):
        if not reuse:
            backend, frontend = available_ports()
            settings = {"backend_port": backend, "frontend_port": frontend, **(plan or {})}
            (tmp_path / "plan.json").write_text(json.dumps(settings))
            (tmp_path / "driver.py").write_text(DRIVER_CODE)
            (tmp_path / "service.py").write_text(SERVICE_CODE)
            (tmp_path / "setup_fixture.py").write_text("from pathlib import Path\nimport json,os,subprocess,sys,time\np=Path(sys.argv[1])\nif json.loads((p/'plan.json').read_text()).get('slow_setup'):\n child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n (p/'setup.child').write_text(str(child.pid))\n (p/'setup-start.events').write_text(json.dumps({'pid':os.getpid()})+'\\n')\n time.sleep(10)\n(p/'dependencies.ready').touch()\nwith (p/'setup.events').open('a') as f: f.write('prepared\\n')\n")
        output = (tmp_path / f"supervisor-{len(processes)}.log").open("w")
        process = subprocess.Popen([sys.executable, str(tmp_path / "driver.py"), str(ROOT / "scripts/local_supervisor.py"), str(tmp_path)],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        output.close()
        processes.append(process)
        return process, tmp_path
    yield launch
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    for events in tmp_path.glob("*.events"):
        if events.name in {"browser.events", "setup.events"}:
            continue
        for line in events.read_text().splitlines():
            try:
                os.killpg(json.loads(line)["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_backend_frontend_and_worker_restart_and_browser_opens_once(runtime):
    process, root = runtime({"open_browser": True, "backend": {"crashes": 1}, "frontend": {"crashes": 1}, "research": {"crashes": 1}})
    def recovered():
        state = read_json(root / "backend/data/runtime/pids/local-runtime.json")
        return state if state.get("status") == "ready" and all(s["restarts"] == 1 for s in state.get("services", {}).values()) else None
    state = wait_for(recovered)
    assert all(s["healthy"] for s in state["services"].values())
    wait_for(lambda: (root / "browser.events").exists())
    assert len((root / "browser.events").read_text().splitlines()) == 1
    for name in ("backend", "frontend", "research"):
        events = [json.loads(line) for line in (root / f"{name}.events").read_text().splitlines()]
        assert len(events) == 2 and events[0]["pid"] != events[1]["pid"]
        assert not process_running(events[0]["pid"])
        assert events[1]["at"] - events[0]["at"] >= .35
        assert "fixture service running" in (root / f"backend/data/runtime/logs/{name}.log").read_text()
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=5) == 130, (root / "supervisor-0.log").read_text()


def test_restart_limit_is_bounded_and_failure_stops_siblings(runtime):
    process, root = runtime({"backend": {"crashes": 20}})
    assert process.wait(timeout=8) == 1
    state = read_json(root / "backend/data/runtime/pids/local-runtime.json")
    assert state["status"] == "failed" and "重启上限" in state["error"]
    assert (root / "backend.count").read_text() == "3"
    assert all(row["pid"] is None for row in state["services"].values())
    assert not list((root / "backend/data/runtime/pids").glob("*.pid"))
    assert not (root / "browser.events").exists()


def test_ctrl_c_kills_owned_descendants_even_if_child_ignores_term(runtime):
    process, root = runtime({"research": {"spawn_child": True, "ignore_term": True}})
    wait_for(lambda: read_json(root / "backend/data/runtime/pids/local-runtime.json").get("status") == "ready")
    child = int((root / "research.child").read_text())
    assert process_running(child)
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=5) == 130, (root / "supervisor-0.log").read_text()
    wait_for(lambda: not process_running(child))
    state = read_json(root / "backend/data/runtime/pids/local-runtime.json")
    assert state["status"] == "stopped"
    assert all(s["pid"] is None for s in state["services"].values())


def test_occupied_port_is_reported_without_touching_its_listener(runtime):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        process, root = runtime({"backend_port": listener.getsockname()[1]})
        assert process.wait(timeout=5) == 1
        assert "未终止任何现有进程" in read_json(root / "backend/data/runtime/pids/local-runtime.json")["error"]
        assert not (root / "setup.events").exists()
        assert not list(root.glob("*.count"))
        client = socket.create_connection(listener.getsockname(), timeout=1)
        client.close()
    finally:
        listener.close()


def test_duplicate_launch_reuses_held_instance_lock_without_setup(runtime):
    first, root = runtime()
    wait_for(lambda: read_json(root / "backend/data/runtime/pids/local-runtime.json").get("status") == "ready")
    prior = read_json(root / "backend/data/runtime/pids/local-runtime.json")
    duplicate, _ = runtime(reuse=True)
    assert duplicate.wait(timeout=5) == 0
    assert first.poll() is None
    assert read_json(root / "backend/data/runtime/pids/local-runtime.json")["pid"] == prior["pid"]
    assert len((root / "setup.events").read_text().splitlines()) == 1
    assert "未重复启动" in (root / "supervisor-1.log").read_text()


def test_lost_dependencies_run_setup_before_restart(runtime):
    process, root = runtime({"research": {"crashes": 1, "broken_deps": True}})
    wait_for(lambda: read_json(root / "backend/data/runtime/pids/local-runtime.json").get("services", {}).get("research", {}).get("restarts") == 1)
    wait_for(lambda: read_json(root / "backend/data/runtime/pids/local-runtime.json").get("status") == "ready")
    assert len((root / "setup.events").read_text().splitlines()) == 2
    assert (root / "dependencies.ready").exists()
    assert process.poll() is None


def test_unresponsive_ready_endpoint_is_restarted_with_same_budget(runtime):
    process, root = runtime({"backend": {"unhealthy": True}, "ready_timeout": .2, "restart_limit": 1})
    assert process.wait(timeout=6) == 1
    assert (root / "backend.count").read_text() == "2"
    assert "就绪检查持续失败" in read_json(root / "backend/data/runtime/pids/local-runtime.json")["error"]


def test_default_reload_is_disabled_and_browser_flag_can_disable(monkeypatch, tmp_path):
    config = supervisor.Config(tmp_path)
    assert "--reload" not in supervisor.default_services(config)[0].command
    config.reload = True
    assert "--reload" in supervisor.default_services(config)[0].command
    monkeypatch.setenv("SVA_OPEN_BROWSER", "false")
    assert supervisor.env_bool("SVA_OPEN_BROWSER", True) is False
    monkeypatch.setenv("SVA_OPEN_BROWSER", "maybe")
    with pytest.raises(supervisor.StartupError):
        supervisor.env_bool("SVA_OPEN_BROWSER", True)


def test_supervisor_roots_are_fixed_and_setup_gets_canonical_path(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKVIEW_DATA_DIR", "../portable data")
    config = supervisor.Config(tmp_path)
    runner = supervisor.Supervisor(config, services=[])
    expected = tmp_path / "portable data"
    assert runner.log_dir == expected / "runtime/logs"
    assert runner.pid_dir == expected / "runtime/pids"
    monkeypatch.setenv("STOCKVIEW_DATA_DIR", "changed-after-start")
    seen = []
    class Prepared:
        returncode = 0
        def poll(self): return 0
    def start(*args, **kwargs):
        seen.append(kwargs["env"]["STOCKVIEW_DATA_DIR"])
        return Prepared()
    monkeypatch.setattr(supervisor.subprocess, "Popen", start)
    monkeypatch.setattr(runner, "stop_processes", lambda processes: None)
    runner.prepare()
    assert seen == [str(expected)] and config.data_dir == expected


def test_held_legacy_instance_lock_prevents_duplicate_upgrade_start(tmp_path):
    legacy = tmp_path / "pids/local-runtime.lock"
    legacy.parent.mkdir()
    legacy.with_name("local-runtime.json").write_text(json.dumps({"pid": os.getpid(), "status": "ready"}))
    with legacy.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = supervisor.Supervisor(supervisor.Config(tmp_path), setup_command=["must-not-run"])
        assert runner.run() == 0
    assert not runner.pid_dir.exists()
    assert legacy.exists()


def test_status_does_not_prepare_or_launch_and_stale_pid_is_not_killed(tmp_path):
    config = supervisor.Config(tmp_path, open_browser=False)
    (tmp_path / "pids").mkdir()
    (tmp_path / "pids/local-runtime.json").write_text(json.dumps({"pid": os.getpid(), "status": "ready"}))
    runner = supervisor.Supervisor(config, setup_command=["must-not-run"])
    assert runner.run(status_only=True) == 1
    assert os.getpid() > 0
    assert not (tmp_path / "logs").exists()


def test_data_root_lock_spans_preparation_recovery_and_final_cleanup(tmp_path, monkeypatch):
    runner = supervisor.Supervisor(supervisor.Config(tmp_path, open_browser=False), services=[])
    stages = []

    def assert_locked(stage):
        with pytest.raises(supervisor.DataPortabilityError, match="Data directory is in use"):
            with supervisor.data_directory_lock(runner.config.data_dir, exclusive=True):
                pytest.fail("offline migration must not run while the launcher owns the root")
        stages.append(stage)

    def monitor():
        # No child is running here: the launcher must retain ownership during
        # a simultaneous API/worker crash and the subsequent recovery setup.
        assert_locked("recovery")
        runner.prepare()
        runner.request_stop(signal.SIGTERM)

    monkeypatch.setattr(supervisor, "check_ports", lambda config: None)
    monkeypatch.setattr(runner, "prepare", lambda: assert_locked("prepare"))
    monkeypatch.setattr(runner, "monitor", monitor)
    monkeypatch.setattr(runner, "stop_processes", lambda processes: assert_locked("cleanup"))
    assert runner.run() == 128 + signal.SIGTERM
    assert stages == ["prepare", "recovery", "prepare", "cleanup"]
    with supervisor.data_directory_lock(runner.config.data_dir, exclusive=True):
        assert read_json(runner.state_path)["status"] == "stopped"


@pytest.mark.parametrize("status_only", [False, True])
def test_migration_lock_blocks_launcher_before_any_setup_or_status_write(tmp_path, capsys, status_only):
    runner = supervisor.Supervisor(supervisor.Config(tmp_path), setup_command=["must-not-run"])
    with supervisor.data_directory_lock(runner.config.data_dir, exclusive=True):
        assert runner.run(status_only=status_only) == 1
    assert not runner.pid_dir.exists()
    assert not runner.log_dir.exists()
    assert "等待迁移完成" in capsys.readouterr().out


def test_status_only_releases_data_root_lock_without_creating_business_files(tmp_path):
    runner = supervisor.Supervisor(supervisor.Config(tmp_path), setup_command=["must-not-run"])
    assert runner.run(status_only=True) == 1
    with supervisor.data_directory_lock(runner.config.data_dir, exclusive=True):
        assert not runner.state_path.exists()
        assert not (runner.config.data_dir / "users.db").exists()


def test_ctrl_c_during_dependency_preparation_stops_installer_group(runtime):
    process, root = runtime({"slow_setup": True})
    wait_for(lambda: (root / "setup.child").exists())
    child = int((root / "setup.child").read_text())
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=5) == 130, (root / "supervisor-0.log").read_text()
    wait_for(lambda: not process_running(child))
    assert not list(root.glob("*.count"))
    assert read_json(root / "backend/data/runtime/pids/local-runtime.json")["status"] == "stopped"


def test_continuous_health_restarts_budget_clock_after_unhealthy_interval(tmp_path, monkeypatch):
    service = supervisor.Service("backend", [], tmp_path, "http://fixture.invalid/")
    service.failures = 3
    service.started = -1000
    runner = supervisor.Supervisor(supervisor.Config(tmp_path, stable_seconds=120), services=[service])
    healthy = [True]
    monkeypatch.setattr(runner, "health_ok", lambda _: healthy[0])
    runner.probe_health(service, 0)
    assert service.failures == 3
    healthy[0] = False
    runner.probe_health(service, 60)
    assert service.healthy_since is None
    healthy[0] = True
    runner.probe_health(service, 121)
    assert service.healthy_since == 121 and service.failures == 3
    runner.probe_health(service, 238)
    assert service.failures == 3
    runner.probe_health(service, 241)
    assert service.failures == 0


def test_state_snapshots_are_throttled_but_transitions_publish_immediately(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(supervisor.time, "time", lambda: clock[0])
    runner = supervisor.Supervisor(supervisor.Config(tmp_path), services=[])
    runner.pid_dir.mkdir(parents=True)
    runner.status = "ready"
    runner.snapshot()
    clock[0] = 2.9
    runner.snapshot()
    assert read_json(runner.state_path)["updated_at"] == 0
    clock[0] = 3.0
    runner.snapshot()
    assert read_json(runner.state_path)["updated_at"] == 3.0
    clock[0] = 3.1
    runner.status = "recovering"
    runner.snapshot(force=True)
    assert read_json(runner.state_path)["updated_at"] == 3.1
    assert read_json(runner.state_path)["status"] == "recovering"


def test_steady_http_checks_slow_down_while_crash_detection_stays_fast(runtime):
    process, root = runtime({"health_interval": 3, "state_interval": 3})
    state_path = root / "backend/data/runtime/pids/local-runtime.json"
    wait_for(lambda: read_json(state_path).get("status") == "ready")
    written_at = state_path.stat().st_mtime_ns
    time.sleep(.5)
    assert state_path.stat().st_mtime_ns == written_at
    wait_for(lambda: len((root / "backend.health").read_text().splitlines()) >= 2, timeout=4)
    probes = [float(value) for value in (root / "backend.health").read_text().splitlines()]
    assert probes[1] - probes[0] >= 2.8
    assert len(probes) == 2
    old_pid = read_json(state_path)["services"]["research"]["pid"]
    crashed_at = time.monotonic()
    os.kill(old_pid, signal.SIGKILL)
    wait_for(lambda: read_json(state_path)["services"]["research"]["last_exit"] == -signal.SIGKILL, timeout=1)
    assert time.monotonic() - crashed_at < 1
    wait_for(lambda: read_json(state_path)["status"] == "ready" and read_json(state_path)["services"]["research"]["pid"] != old_pid)
    assert process.poll() is None
