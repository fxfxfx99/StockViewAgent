"""Isolated, single-worker public trial with a supervised Cloudflare quick tunnel."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
TRIAL = ROOT / ".public-trial"
RUNTIME = TRIAL / "runtime"
STATE = TRIAL / "state.json"
PYTHON = ROOT / "backend/.venv/bin/python"
PORT = 8088
URL_RE = re.compile(r"https://(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com(?![\w.-])")


def private_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def process_record(pid: int) -> dict:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True, text=True, check=False,
    )
    return {"pid": pid, "identity": result.stdout.strip()}


def is_alive(record: dict | None) -> bool:
    if not record or not record.get("identity"):
        return False
    return process_record(int(record["pid"]))["identity"] == record["identity"]


def clean_environment() -> dict[str, str]:
    # Never inherit local API credentials, provider settings or auth overrides.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def parse_macos_proxy(output: str) -> dict[str, str]:
    values = dict(re.findall(r"^\s*(HTTP\w+)\s*:\s*(.*?)\s*$", output, re.MULTILINE))
    result = {}
    for prefix, env_key in (("HTTP", "HTTP_PROXY"), ("HTTPS", "HTTPS_PROXY")):
        host = values.get(prefix + "Proxy", "")
        port = values.get(prefix + "Port", "")
        if values.get(prefix + "Enable") != "1" or not port.isdigit() or not 1 <= int(port) <= 65535:
            continue
        if not host or any(char.isspace() or char in "/@?#" for char in host):
            continue
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        result[env_key] = f"http://{host}:{port}"
    return result


def tunnel_environment() -> dict[str, str]:
    env = clean_environment()
    if sys.platform == "darwin":
        result = subprocess.run(["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, check=False)
        env.update(parse_macos_proxy(result.stdout))
    else:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if os.environ.get(key):
                env[key] = os.environ[key]
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    return env


def find_node() -> Path:
    found = shutil.which("node")
    if found:
        return Path(found)
    for candidate in sorted((Path.home() / ".cache/codex-runtimes").glob("**/node/bin/node"), reverse=True):
        if os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("未找到 Node.js，请安装 Node.js 20+（含 npm）后重试。")


def find_cloudflared() -> Path:
    bundled = TRIAL / "bin/cloudflared"
    if os.access(bundled, os.X_OK):
        return bundled
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    raise RuntimeError("未找到 cloudflared。macOS 可运行 brew install cloudflared；或把官方二进制放到 .public-trial/bin/cloudflared。")


def prepare_runtime() -> None:
    if not PYTHON.is_file():
        raise RuntimeError("请先运行 ./scripts/setup.sh，建立 backend/.venv。")
    node = find_node()
    env = clean_environment()
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    # Explicit empty value overrides frontend/.env and keeps all API calls same-origin.
    env["VITE_API_BASE_URL"] = ""
    npm = shutil.which("npm", path=env["PATH"])
    vite = ROOT / "frontend/node_modules/vite/bin/vite.js"
    if npm:
        build_command = [npm, "run", "build"]
    elif vite.is_file():
        build_command = [str(node), str(vite), "build"]
    else:
        raise RuntimeError("未找到 npm 或已安装的 Vite，请先准备前端依赖。")
    print("正在构建生产前端并准备独立试用目录…", flush=True)
    with (TRIAL / "build.log").open("w") as log:
        result = subprocess.run(build_command, cwd=ROOT / "frontend", env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError("前端构建失败，请查看 .public-trial/build.log。")
    for source, destination in (
        (ROOT / "backend/app", RUNTIME / "backend/app"),
        (ROOT / "frontend/dist", RUNTIME / "frontend/dist"),
    ):
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "deploy/trial_app.py", RUNTIME / "backend/trial_app.py")
    credentials_file = TRIAL / "admin-credentials.json"
    if not credentials_file.exists():
        private_json(credentials_file, {
            "admin": {"username": "admin", "password": secrets.token_urlsafe(30)},
            "user": {"username": "user", "password": secrets.token_urlsafe(30)},
            "jwt_secret": secrets.token_urlsafe(48),
        })
    credentials_file.chmod(0o600)
    credentials = json.loads(credentials_file.read_text())
    settings = {
        "AUTH_REQUIRED": "true",
        "AUTH_REGISTRATION_ENABLED": "true",
        "AUTH_JWT_SECRET": credentials["jwt_secret"],
        "AUTH_BOOTSTRAP_ADMIN_PASSWORD": credentials["admin"]["password"],
        "AUTH_BOOTSTRAP_USER_PASSWORD": credentials["user"]["password"],
        "ENABLE_SCHEDULER": "false",
        "ENABLE_STARTUP_DATA_CHECK": "false",
        "COMPANY_AUTO_REFRESH_ENABLED": "false",
        "CORS_ALLOWED_ORIGINS": "",
    }
    env_file = RUNTIME / "backend/.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
    env_file.chmod(0o600)
    data = RUNTIME / "backend/data"
    data.mkdir(exist_ok=True)
    integrations = data / "integrations.json"
    if not integrations.exists():
        private_json(integrations, {"market_data_provider": "public"})


def health_ok() -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{PORT}/api/health", timeout=2) as response:
            return response.status == 200 and json.load(response).get("status") == "ok"
    except (OSError, ValueError):
        return False


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def supervise(run_id: str) -> None:
    children = {}
    stopping = False
    state = {"run_id": run_id, "status": "starting", "started_at": time.time(), "supervisor": process_record(os.getpid()), "processes": {}}

    def stop_requested(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    private_json(STATE, state)
    try:
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            children["caffeinate"] = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], env=clean_environment())
        tunnel_log = TRIAL / "tunnel.log"
        with tunnel_log.open("w") as log:
            children["tunnel"] = subprocess.Popen([
                str(find_cloudflared()), "tunnel", "--no-autoupdate", "--http-host-header", "localhost",
                "--protocol", "http2", "--url", f"http://127.0.0.1:{PORT}",
            ], stdout=log, stderr=subprocess.STDOUT, env=tunnel_environment())
        state["processes"] = {name: process_record(process.pid) for name, process in children.items()}
        private_json(STATE, state)
        deadline = time.monotonic() + 90
        while not stopping and time.monotonic() < deadline:
            if children["tunnel"].poll() is not None:
                raise RuntimeError("隧道进程启动失败，请查看 .public-trial/tunnel.log。")
            match = URL_RE.search(tunnel_log.read_text(errors="replace"))
            if match:
                state["url"] = match.group()
                break
            time.sleep(0.25)
        if stopping:
            return
        if not state.get("url"):
            raise RuntimeError("获取公网试用地址超时，请查看 .public-trial/tunnel.log。")
        env_file = RUNTIME / "backend/.env"
        contents = env_file.read_text()
        contents = re.sub(r"^CORS_ALLOWED_ORIGINS=.*$", "CORS_ALLOWED_ORIGINS=" + state["url"], contents, flags=re.MULTILINE)
        env_file.write_text(contents)
        env_file.chmod(0o600)
        private_json(STATE, state)
        with (TRIAL / "server.log").open("w") as log:
            children["server"] = subprocess.Popen([
                str(PYTHON), "-m", "uvicorn", "trial_app:app", "--host", "127.0.0.1", "--port", str(PORT),
                "--no-proxy-headers", "--no-access-log",
            ], cwd=RUNTIME / "backend", env=clean_environment(), stdout=log, stderr=subprocess.STDOUT)
        state["processes"]["server"] = process_record(children["server"].pid)
        private_json(STATE, state)
        deadline = time.monotonic() + 60
        while not stopping and time.monotonic() < deadline:
            if any(children[name].poll() is not None for name in ("server", "tunnel")):
                raise RuntimeError("试用服务启动失败，请查看 .public-trial/server.log 和 tunnel.log。")
            if health_ok():
                state["status"] = "running"
                state["ready_at"] = time.time()
                private_json(STATE, state)
                break
            time.sleep(0.25)
        if not stopping and state["status"] != "running":
            raise RuntimeError("本机试用服务健康检查超时，请查看 .public-trial/server.log。")
        while not stopping:
            if any(children[name].poll() is not None for name in ("server", "tunnel")):
                raise RuntimeError("服务或隧道进程退出；执行 start 重新启动。")
            time.sleep(0.5)
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = str(exc)
    finally:
        for process in reversed(list(children.values())):
            stop_process(process)
        state["exit_codes"] = {name: process.returncode for name, process in children.items()}
        state["status"] = "stopped" if stopping else "failed"
        state["stopped_at"] = time.time()
        private_json(STATE, state)


def stop() -> None:
    state = read_state()
    supervisor = state.get("supervisor")
    if is_alive(supervisor):
        os.kill(supervisor["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 30
        while is_alive(supervisor) and time.monotonic() < deadline:
            time.sleep(0.2)
    # Recover only exactly identified recorded children if the supervisor died.
    for record in [supervisor, *state.get("processes", {}).values()]:
        if is_alive(record):
            os.kill(record["pid"], signal.SIGTERM)
            time.sleep(0.2)
            if is_alive(record):
                os.kill(record["pid"], signal.SIGKILL)
    state = read_state()
    state.update(status="stopped", stopped_at=time.time())
    private_json(STATE, state)
    print("公网试用已停止；独立账户和数据已保留。")


def status() -> None:
    state = read_state()
    if state.get("status") == "running" and is_alive(state.get("supervisor")):
        print("公网试用运行中：" + state.get("url", ""))
        print("本机健康检查：" + ("通过" if health_ok() else "未通过"))
    else:
        current = state.get("status", "未启动")
        if current in {"starting", "running"} and not is_alive(state.get("supervisor")):
            current = "管理进程已退出"
        print("公网试用状态：" + current)
        if state.get("error"):
            print(state["error"])


def start() -> None:
    state = read_state()
    if is_alive(state.get("supervisor")):
        status()
        return
    if any(is_alive(record) for record in state.get("processes", {}).values()):
        raise RuntimeError("存在上次试用的残留进程，请先执行 stop。")
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", PORT))
        except OSError as exc:
            raise RuntimeError(f"端口 {PORT} 已被占用；启动器不会停止其他进程。") from exc
    find_cloudflared()
    prepare_runtime()
    run_id = uuid.uuid4().hex
    with (TRIAL / "supervisor.log").open("w") as log:
        supervisor = subprocess.Popen(
            [str(PYTHON), str(Path(__file__).resolve()), "_supervise", run_id],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=clean_environment(), close_fds=True,
        )
    deadline = time.monotonic() + 160
    while time.monotonic() < deadline:
        state = read_state()
        if state.get("run_id") == run_id:
            if state.get("status") == "running" and health_ok():
                print("公网试用地址：" + state["url"])
                print("本机健康检查已通过；请用此链接另行验证公网访问。")
                print("管理凭据仅保存在 .public-trial/admin-credentials.json（权限 600）。")
                return
            if state.get("status") in {"failed", "stopped"}:
                raise RuntimeError(state.get("error", "试用启动已停止。"))
        if supervisor.poll() is not None:
            raise RuntimeError("管理进程退出，请查看 .public-trial/supervisor.log。")
        time.sleep(0.5)
    supervisor.terminate()
    raise RuntimeError("试用启动超时，请查看 .public-trial/ 日志。")


def main() -> int:
    parser = argparse.ArgumentParser(description="管理独立公网试用（start / status / stop）")
    parser.add_argument("command", choices=("start", "status", "stop", "_supervise"), nargs="?", default="status")
    parser.add_argument("run_id", nargs="?")
    args = parser.parse_args()
    os.umask(0o077)
    TRIAL.mkdir(exist_ok=True, mode=0o700)
    TRIAL.chmod(0o700)
    if args.command == "_supervise":
        if not args.run_id:
            return 2
        supervise(args.run_id)
        return 0
    try:
        with (TRIAL / "control.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            {"start": start, "status": status, "stop": stop}[args.command]()
        return 0
    except BlockingIOError:
        print("另一条试用管理命令正在执行，请稍后重试。", file=sys.stderr)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print("错误：" + str(exc), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
