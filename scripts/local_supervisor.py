#!/usr/bin/env python3
"""Local process supervision; stdlib only, no package installs outside setup.sh.

The held flock is the instance identity. PID files are status, never permission to
kill a process. Only Popen children/session groups owned by this run are signaled.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import fcntl
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Callable
import urllib.request
import webbrowser

_PATHS_FILE = Path(__file__).resolve().parent.parent / "backend/app/runtime_paths.py"
_PATHS_SPEC = importlib.util.spec_from_file_location("stockview_runtime_paths", _PATHS_FILE)
_PATHS = importlib.util.module_from_spec(_PATHS_SPEC)
_PATHS_SPEC.loader.exec_module(_PATHS)

# This module is stdlib-only, so the lifetime lock is available even before
# setup.sh has created the project's virtual environment.
sys.path.insert(0, str(_PATHS_FILE.parents[1]))
from app.storage.data_portability import DataPortabilityError, data_directory_lock


class StartupError(RuntimeError):
    pass


@dataclass
class Config:
    root: Path
    data_dir: Path = field(init=False)
    backend_port: int = 8001
    frontend_port: int = 5175
    frontend_host: str = "127.0.0.1"
    reload: bool = False
    open_browser: bool = True
    restart_limit: int = 5
    restart_base_delay: float = 1
    restart_max_delay: float = 30
    stable_seconds: float = 120
    ready_timeout: float = 90
    stop_timeout: float = 12
    poll_interval: float = .2
    health_interval: float = 3
    startup_health_interval: float = .25
    state_interval: float = 3
    startup_state_interval: float = .5

    def __post_init__(self):
        self.root = self.root.expanduser().resolve()
        self.data_dir = _PATHS.resolve_data_dir(self.root / "backend")

    @property
    def browser_url(self):
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(self.frontend_host, self.frontend_host)
        return f"http://{'[' + host + ']' if ':' in host else host}:{self.frontend_port}/"


@dataclass
class Service:
    name: str
    command: list[str]
    cwd: Path
    health_url: str | None = None
    process: subprocess.Popen | None = field(default=None, init=False)
    started: float = field(default=0, init=False)
    next_start: float = field(default=0, init=False)
    failures: int = field(default=0, init=False)
    restarts: int = field(default=0, init=False)
    healthy: bool = field(default=False, init=False)
    healthy_since: float | None = field(default=None, init=False)
    unhealthy_since: float | None = field(default=None, init=False)
    next_health_check: float = field(default=0, init=False)
    ever_healthy: bool = field(default=False, init=False)
    last_exit: int | None = field(default=None, init=False)


def default_services(config: Config) -> list[Service]:
    backend, frontend = config.root / "backend", config.root / "frontend"
    python = str(backend / ".venv/bin/python")
    command = [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
               "--port", str(config.backend_port), "--timeout-graceful-shutdown", "10"]
    if config.reload:
        command.append("--reload")
    return [Service("backend", command, backend, f"http://127.0.0.1:{config.backend_port}/api/ready"),
            Service("frontend", ["node", "./node_modules/vite/bin/vite.js", "--host", config.frontend_host,
                                  "--port", str(config.frontend_port), "--strictPort"], frontend, config.browser_url),
            Service("research", [python, "-m", "app.research_worker"], backend)]


def dependencies_ok(root: Path) -> bool:
    python = root / "backend/.venv/bin/python"
    checks = [([str(python), str(root / "scripts/check_dependencies.py"), str(root / "backend/requirements.txt")], root),
              (["node", "-e", "require('rollup/dist/native.js'); require.resolve('react'); require.resolve('@vitejs/plugin-react'); require.resolve('vite')"], root / "frontend")]
    try:
        return all(subprocess.run(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=20).returncode == 0 for command, cwd in checks)
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_ports(config: Config) -> None:
    listeners = []
    try:
        for name, host, port in (("后端", "127.0.0.1", config.backend_port),
                                  ("前端", config.frontend_host, config.frontend_port)):
            if not 1 <= port <= 65535:
                raise StartupError(f"{name}端口须为 1–65535 的整数")
            listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
            listeners.append(listener)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((host, port))
                listener.listen(1)
            except OSError as exc:
                raise StartupError(f"{name}端口 {host}:{port} 已占用或不可用：{exc}。未终止任何现有进程；请调整 BACKEND_PORT / FRONTEND_PORT。") from exc
    finally:
        for listener in listeners:
            listener.close()


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can report EPERM for a group that disappeared between poll and
        # killpg. Never escalate permissions or signal a recycled foreign group.
        if process.poll() is None:
            process.send_signal(sig)


def group_alive(process: subprocess.Popen) -> bool:
    process.poll()  # Reap the direct child before checking its remaining group.
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return process.poll() is None


class Supervisor:
    def __init__(self, config: Config, *, services: list[Service] | None = None,
                 setup_command: list[str] | None = None,
                 dependency_check: Callable[[], bool] | None = None,
                 browser: Callable[[str], object] | None = None):
        self.config = config
        self.services = services if services is not None else default_services(config)
        self.setup_command = setup_command or ["bash", str(config.root / "scripts/setup.sh")]
        self.dependency_check = dependency_check or (lambda: dependencies_ok(config.root))
        self.browser = browser or (lambda url: webbrowser.open(url, new=2))
        self.pid_dir = config.data_dir / "runtime/pids"
        self.log_dir = config.data_dir / "runtime/logs"
        self.state_path = self.pid_dir / "local-runtime.json"
        self.stopping = False
        self.stop_signal = None
        self.status = "starting"
        self.error = None
        self.started_at = time.time()
        self.opened = False
        self.preparing: subprocess.Popen | None = None
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._loggers: dict[str, logging.Logger] = {}
        self._next_snapshot = 0.0

    def log(self, message: str):
        print(f"[StockViewAgent] {message}", flush=True)

    def snapshot(self, *, force=False):
        now = time.monotonic()
        if not force and now < self._next_snapshot:
            return
        payload = {"pid": os.getpid(), "started_at": self.started_at, "updated_at": time.time(),
                   "status": self.status, "url": self.config.browser_url, "error": self.error,
                   "backend_port": self.config.backend_port, "frontend_port": self.config.frontend_port,
                   "reload": self.config.reload,
                   "data_dir": str(self.config.data_dir),
                   "services": {s.name: {"pid": s.process.pid if s.process and s.process.poll() is None else None,
                       "healthy": s.healthy, "restarts": s.restarts, "consecutive_failures": s.failures,
                       "last_exit": s.last_exit, "retry_in_seconds": round(max(0, s.next_start - time.monotonic()), 2)} for s in self.services}}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.state_path)
        self._next_snapshot = now + (self.config.startup_state_interval if self.status == "starting" else self.config.state_interval)

    def request_stop(self, sig, frame=None):
        self.stop_signal = sig
        self.stopping = True

    def prepare(self):
        self.log("检查并修复运行依赖（仅使用 requirements.txt 与 package-lock.json）…")
        env = {**os.environ, "STOCKVIEW_DATA_DIR": str(self.config.data_dir)}
        self.preparing = subprocess.Popen(self.setup_command, cwd=self.config.root, env=env, start_new_session=True)
        deadline = time.monotonic() + 600
        try:
            while self.preparing.poll() is None:
                if self.stopping:
                    return
                if time.monotonic() > deadline:
                    raise StartupError("依赖准备超过 10 分钟；请检查网络和安装日志后重新启动。")
                time.sleep(self.config.poll_interval)
            if self.preparing.returncode:
                raise StartupError("依赖准备失败，请查看安装日志；未修改源码或业务数据。")
        finally:
            self.stop_processes([self.preparing])
            self.preparing = None

    def pump_log(self, service: Service, process: subprocess.Popen):
        logger = self._loggers[service.name]
        try:
            for line in iter(process.stdout.readline, b""):
                logger.info(line.decode("utf-8", errors="replace").rstrip())
        finally:
            process.stdout.close()

    def launch(self, service: Service):
        env = dict(os.environ)
        env.update(VITE_DEV_BACKEND_URL=f"http://127.0.0.1:{self.config.backend_port}",
                   VITE_DEV_PORT=str(self.config.frontend_port), PYTHONUNBUFFERED="1",
                   STOCKVIEW_DATA_DIR=str(self.config.data_dir))
        if service.name not in self._loggers:
            logger = logging.getLogger(f"stockview.local.{os.getpid()}.{service.name}")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = RotatingFileHandler(self.log_dir / f"{service.name}.log", maxBytes=5_000_000, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger.addHandler(handler)
            self._loggers[service.name] = logger
        try:
            process = subprocess.Popen(service.command, cwd=service.cwd, env=env,
                start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            self.schedule_restart(service, f"无法启动：{exc}")
            return
        service.process = process
        service.started = time.monotonic()
        service.unhealthy_since = service.started
        service.healthy_since = None
        service.healthy = False
        service.ever_healthy = False
        service.next_health_check = 0
        service.next_start = 0
        (self.pid_dir / f"{service.name}.pid").write_text(str(process.pid) + "\n")
        threading.Thread(target=self.pump_log, args=(service, process), daemon=True).start()
        self.log(f"已启动 {service.name}（PID {process.pid}）；日志 {self.log_dir / (service.name + '.log')}")
        self.snapshot(force=True)

    def stop_processes(self, processes: list[subprocess.Popen]):
        for process in processes:
            signal_group(process, signal.SIGTERM)
        deadline = time.monotonic() + self.config.stop_timeout
        while any(group_alive(p) for p in processes) and time.monotonic() < deadline:
            time.sleep(.05)
        for process in processes:
            if group_alive(process):
                signal_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.log(f"进程组 {process.pid} 正在等待系统回收")

    def retire(self, service: Service):
        if service.process:
            self.stop_processes([service.process])
            path = self.pid_dir / f"{service.name}.pid"
            try:
                if path.read_text().strip() == str(service.process.pid):
                    path.unlink()
            except FileNotFoundError:
                pass
            service.process = None
        service.healthy = False
        service.healthy_since = None

    def schedule_restart(self, service: Service, reason: str):
        self.retire(service)
        service.failures += 1
        if service.failures > self.config.restart_limit:
            raise StartupError(f"{service.name} 连续恢复失败，已达到 {self.config.restart_limit} 次重启上限：{reason}。日志：{self.log_dir / (service.name + '.log')}")
        delay = min(self.config.restart_max_delay, self.config.restart_base_delay * 2 ** (service.failures - 1))
        service.next_start = time.monotonic() + delay
        self.status = "recovering"
        self.log(f"{service.name} {reason}；{delay:g} 秒后第 {service.failures}/{self.config.restart_limit} 次重启")
        self.snapshot(force=True)

    def health_ok(self, service: Service):
        if not service.health_url:
            return True
        try:
            with self._http.open(service.health_url, timeout=.5) as response:
                return response.status == 200
        except (OSError, ValueError):
            return False

    def probe_health(self, service: Service, now: float):
        if now < service.next_health_check:
            return
        service.healthy = self.health_ok(service)
        if service.healthy:
            service.ever_healthy = True
            service.unhealthy_since = None
            if service.healthy_since is None:
                service.healthy_since = now
            if now - service.healthy_since >= self.config.stable_seconds:
                service.failures = 0
        else:
            # Any observed unhealthy interval restarts the continuous-health
            # clock, even when the OS process has been alive for hours.
            service.healthy_since = None
            if service.unhealthy_since is None:
                service.unhealthy_since = now
        interval = self.config.health_interval if service.ever_healthy else self.config.startup_health_interval
        service.next_health_check = now + interval

    def open_browser(self, url=None):
        if not self.config.open_browser or self.opened:
            return
        self.opened = True
        def open_once():
            try:
                if self.browser(url or self.config.browser_url) is False:
                    self.log(f"浏览器未自动打开，请访问 {url or self.config.browser_url}")
            except Exception:
                self.log(f"浏览器未自动打开，请访问 {url or self.config.browser_url}")
        threading.Thread(target=open_once, daemon=True).start()

    def monitor(self):
        while not self.stopping:
            prior_status = self.status
            for service in self.services:
                if self.stopping:
                    break
                if service.process is None:
                    if time.monotonic() < service.next_start:
                        continue
                    dependencies_ready = self.dependency_check()
                    if self.stopping:
                        break
                    if not dependencies_ready:
                        self.prepare()
                    if not self.stopping:
                        service.restarts += 1
                        self.launch(service)
                    continue
                code = service.process.poll()
                if code is not None:
                    service.last_exit = code
                    self.schedule_restart(service, f"意外退出（{code}）")
                    continue
                now = time.monotonic()
                self.probe_health(service, now)
                if not service.healthy and service.unhealthy_since is not None and now - service.unhealthy_since >= self.config.ready_timeout:
                    self.schedule_restart(service, "就绪检查持续失败")
            if self.stopping:
                break
            ready = all(s.process is not None and s.healthy for s in self.services)
            if ready and self.status != "ready":
                self.status = "ready"
                self.log(f"前后端与研究 worker 已就绪：{self.config.browser_url}")
                self.log("首次使用只需在配置台填写大模型 API；免费行情源已默认启用。停止：Ctrl+C")
                self.open_browser()
            elif not ready and self.status == "ready":
                self.status = "recovering"
            self.snapshot(force=self.status != prior_status)
            time.sleep(self.config.poll_interval)

    def run(self, *, status_only=False) -> int:
        # Keep the root protected when all children are temporarily down, and
        # until their process groups have been reaped during final cleanup.
        try:
            with data_directory_lock(self.config.data_dir):
                return self._run_locked(status_only=status_only)
        except (DataPortabilityError, OSError) as exc:
            self.error = str(exc)
            self.status = "failed"
            self.log(f"数据目录暂不可用；请等待迁移完成并检查目录权限后重试：{exc}")
            return 1

    def _run_locked(self, *, status_only=False) -> int:
        # An instance started by the previous launcher still owns its old lock.
        # Do not delete its files or start another set of processes after upgrade.
        legacy_lock = self.config.root / "pids/local-runtime.lock"
        if legacy_lock.exists():
            with legacy_lock.open("r+") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    existing = read_state(legacy_lock.with_name("local-runtime.json"))
                    self.log(f"旧启动器实例仍在运行（PID {existing.get('pid', '未知')}）；请先停止旧实例，再使用数据目录内的新启动器。未重复启动。")
                    return 0
        self.pid_dir.mkdir(parents=True, exist_ok=True)
        with (self.pid_dir / "local-runtime.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                existing = read_state(self.state_path)
                self.log(f"此项目已在运行（{existing.get('status', '正在启动')}，监督器 PID {existing.get('pid', '未知')}）：{existing.get('url', '请稍候')}；本次未重复启动。")
                if not status_only and existing.get("status") == "ready":
                    self.open_browser(existing.get("url"))
                return 0
            if status_only:
                self.log("此项目当前未运行；执行 ./start.sh 启动。")
                return 1
            old_handlers = {sig: signal.signal(sig, self.request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
            self.log_dir.mkdir(parents=True, exist_ok=True)
            result = 0
            try:
                self.snapshot(force=True)
                check_ports(self.config)
                self.prepare()
                if not self.stopping:
                    for service in self.services:
                        self.launch(service)
                    self.monitor()
            except (StartupError, OSError) as exc:
                self.error = str(exc)
                self.status = "failed"
                self.log(f"启动/恢复失败：{exc}")
                result = 1
            finally:
                self.stopping = True
                processes = [s.process for s in self.services if s.process]
                if self.preparing:
                    processes.append(self.preparing)
                self.stop_processes(processes)
                for service in self.services:
                    if service.process:
                        path = self.pid_dir / f"{service.name}.pid"
                        try:
                            if path.read_text().strip() == str(service.process.pid):
                                path.unlink()
                        except FileNotFoundError:
                            pass
                        service.process = None
                    service.healthy = False
                if self.status != "failed":
                    self.status = "stopped"
                self.snapshot(force=True)
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
                self.log("本次启动的全部服务已停止。")
            return 128 + self.stop_signal if self.stop_signal else result


def env_bool(name: str, default: bool):
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.lower() not in {"true", "false", "1", "0", "yes", "no"}:
        raise StartupError(f"{name} 须为 true 或 false")
    return raw.lower() in {"true", "1", "yes"}


def main():
    parser = argparse.ArgumentParser(description="启动与监督本地前后端及研究 worker")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--reload", action="store_true", help="开启后端开发自动重载，默认关闭")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--status", action="store_true", help="只查看当前项目的运行状态")
    args = parser.parse_args()
    try:
        config = Config(root=args.root.resolve(), backend_port=int(os.environ.get("BACKEND_PORT", "8001")),
            frontend_port=int(os.environ.get("FRONTEND_PORT", "5175")), frontend_host=os.environ.get("FRONTEND_HOST", "127.0.0.1"),
            reload=args.reload or env_bool("SVA_DEV_RELOAD", False),
            open_browser=not args.no_browser and env_bool("SVA_OPEN_BROWSER", True))
        return Supervisor(config).run(status_only=args.status)
    except (StartupError, ValueError) as exc:
        print(f"启动配置错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
