#!/usr/bin/env python
"""Run the whole ASTRION stack with one command.

    python dev.py

Starts the FastAPI backend and the Next.js frontend together, streams both
logs into this terminal with a prefix saying which is which, and shuts both
down cleanly on Ctrl+C. Replaces the two-terminal dance the README otherwise
asks for; `uvicorn` and `npm run dev` still work on their own exactly as before.

Nothing here is part of the application. It starts the same processes with the
same arguments a developer would type, and configures nothing the running
services do not already default to.

Windows notes, since that is where this mostly runs:

- Children are started in their own process group, so a Ctrl+C in this console
  is delivered to *this* script rather than racing the children. Shutdown is
  then explicit and ordered instead of three processes each handling the same
  signal at once.
- `npm` is a `.cmd` shim that spawns `node` as a grandchild, so terminating the
  shim would orphan the server that actually holds port 3000. Shutdown kills
  the whole process tree.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "app" / "frontend"

#: What uvicorn binds to. Loopback only, so nothing on the network can reach
#: a development backend that runs without TLS.
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8000
FRONTEND_PORT = 3000

#: The host the *browser* uses for both servers, and it has to be one host.
#:
#: `localhost` and `127.0.0.1` are the same machine but different registrable
#: hosts, so a page served from one calling an API on the other is a cross-site
#: request. The session cookie is `SameSite=Lax` and is therefore not sent on
#: it: `POST /api/auth/login` sets the cookie, `GET /api/auth/me` does not carry
#: it, and sign-in appears to succeed and then silently does not. The backend's
#: CORS allow-list defaults to `http://localhost:3000`, which is the same
#: choice made from the other side.
#:
#: Nothing about the cookie itself needs relaxing for local work: `localhost`
#: is a trustworthy origin, so a browser stores and returns a `Secure` cookie
#: over plaintext HTTP there. `SESSION_COOKIE_SECURE` stays true.
BROWSER_HOST = "localhost"

#: Where the browser sends API calls. This value also becomes the frontend's
#: `connect-src` in `next.config.ts`, so the client and the Content-Security-
#: Policy cannot disagree about which origin is allowed.
BACKEND_ORIGIN = f"http://{BROWSER_HOST}:{BACKEND_PORT}"

#: Where *this script* polls health. Addressed by the bind address rather than
#: by name, so the check cannot fail on a machine whose `localhost` resolves to
#: `::1` before `127.0.0.1`.
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
FRONTEND_URL = f"http://{BROWSER_HOST}:{FRONTEND_PORT}"

IS_WINDOWS = os.name == "nt"

# Windows hands this process a cp1252 stdout whenever output is redirected, and
# Next.js prints box-drawing and tick glyphs in its own banner. Without this the
# log pumps die on the first such line with UnicodeEncodeError and the launcher
# goes silent while both servers keep running -- the worst possible failure.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

# ANSI is used only if this is a real terminal that will render it. A log file
# or a piped run gets clean text rather than escape codes.
_COLOR = sys.stdout.isatty()


def _enable_windows_ansi() -> None:
    """Turn on virtual-terminal processing so ANSI works in conhost."""
    if not (IS_WINDOWS and _COLOR):
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        # Cosmetic only. A console that refuses VT still gets readable output.
        pass


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


class Service:
    """One child process, its label, and the pump that echoes its output."""

    def __init__(self, name: str, colour: str, argv: list[str], cwd: Path, env: dict[str, str]):
        self.name = name
        self.colour = colour
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.process: subprocess.Popen[str] | None = None

    @property
    def label(self) -> str:
        return _paint(f"{self.name:>8}", self.colour)

    def start(self) -> None:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        self.process = subprocess.Popen(
            self.argv,
            cwd=str(self.cwd),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            print(f"{self.label} | {line.rstrip()}", flush=True)

    @property
    def returncode(self) -> int | None:
        return None if self.process is None else self.process.poll()

    def stop(self) -> None:
        """Stop this service and everything it spawned."""
        if self.process is None or self.process.poll() is not None:
            return

        if IS_WINDOWS:
            # /T takes the children with it - without this, `npm` dies and the
            # `node` process still holding port 3000 survives.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            self.process.terminate()

        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def python_executable() -> str:
    """The interpreter to run the backend with.

    Prefers the repository's own `.venv` so `python dev.py` behaves the same
    whether or not the virtual environment happens to be activated.
    """
    candidate = ROOT / ".venv" / ("Scripts" if IS_WINDOWS else "bin") / (
        "python.exe" if IS_WINDOWS else "python"
    )
    return str(candidate) if candidate.exists() else sys.executable


def port_is_busy(port: str | int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.6)
        return probe.connect_ex((host, int(port))) == 0


def preflight() -> list[str]:
    """Everything that would otherwise fail confusingly a few seconds in."""
    problems: list[str] = []

    if not (ROOT / "app" / "backend" / "main.py").exists():
        problems.append("Backend entry point app/backend/main.py is missing.")

    if not FRONTEND_DIR.exists():
        problems.append(f"Frontend directory not found: {FRONTEND_DIR}")
    elif not (FRONTEND_DIR / "node_modules").exists():
        problems.append(
            "Frontend dependencies are not installed. Run:\n"
            "    cd app/frontend; npm install"
        )

    if shutil.which("npm") is None:
        problems.append("npm was not found on PATH. Install Node.js and reopen the terminal.")

    for name, port in (("Backend", BACKEND_PORT), ("Frontend", FRONTEND_PORT)):
        if port_is_busy(port):
            problems.append(
                f"{name} port {port} is already in use. Stop whatever is listening "
                f"on it, or find it with:\n"
                f"    netstat -ano | findstr :{port}"
            )

    return problems


def build_services() -> list[Service]:
    backend_env = os.environ.copy()
    # Without this uvicorn's output arrives in chunks and the prefixed log
    # stops interleaving usefully with the frontend's.
    backend_env["PYTHONUNBUFFERED"] = "1"
    backend_env.setdefault("PYTHONPATH", str(ROOT))

    frontend_env = os.environ.copy()
    # Point the browser bundle at the backend this script actually started, on
    # the host the browser reaches *it* by — see `BROWSER_HOST`. Set here rather
    # than relied upon from a file so the two always agree.
    frontend_env["NEXT_PUBLIC_API_BASE_URL"] = BACKEND_ORIGIN

    npm = shutil.which("npm") or "npm"

    return [
        Service(
            name="backend",
            colour="36",  # cyan
            argv=[
                python_executable(),
                "-m",
                "uvicorn",
                "app.backend.main:app",
                "--reload",
                "--host",
                BACKEND_HOST,
                "--port",
                str(BACKEND_PORT),
            ],
            cwd=ROOT,
            env=backend_env,
        ),
        Service(
            name="frontend",
            colour="35",  # magenta
            argv=[npm, "run", "dev", "--", "--port", str(FRONTEND_PORT)],
            cwd=FRONTEND_DIR,
            env=frontend_env,
        ),
    ]


def wait_until_healthy(timeout: float = 45.0) -> bool:
    """Poll the backend's own health endpoint until it answers."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BACKEND_URL}/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def banner() -> None:
    print()
    print(_paint("  ASTRION — AI Logistics Support", "1"))
    print(f"  {_paint('backend ', '36')} {BACKEND_ORIGIN}      (health at {BACKEND_URL}/health)")
    print(f"  {_paint('frontend', '35')} {FRONTEND_URL}")
    print()
    print(_paint("  Open the frontend URL. Press Ctrl+C once to stop both.", "2"))
    print()


def _interrupt(_signum: int, _frame: object) -> None:
    """Route a console break into the same shutdown path as Ctrl+C."""
    raise KeyboardInterrupt


def main() -> int:
    _enable_windows_ansi()

    if IS_WINDOWS:
        # Ctrl+Break is delivered as SIGBREAK, whose default action kills this
        # process outright -- which would leave uvicorn and node running and
        # both ports held. Route it through the same ordered shutdown as Ctrl+C.
        try:
            signal.signal(signal.SIGBREAK, _interrupt)  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    problems = preflight()
    if problems:
        print(_paint("Cannot start:", "31"), file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    services = build_services()
    banner()

    for service in services:
        service.start()

    exit_code = 0
    try:
        # Report readiness once, so "is it up yet?" is answered on screen
        # rather than by the user refreshing a browser tab.
        def announce() -> None:
            if wait_until_healthy():
                print(f"{_paint('   ready', '32')} | backend is answering at {BACKEND_URL}/health", flush=True)
            else:
                print(f"{_paint('   ready', '33')} | backend did not answer /health in time - check the log above", flush=True)

        threading.Thread(target=announce, daemon=True).start()

        while True:
            for service in services:
                code = service.returncode
                if code is not None:
                    print()
                    print(
                        _paint(
                            f"{service.name} exited unexpectedly (exit code {code}). "
                            f"Shutting the other service down.",
                            "31",
                        ),
                        file=sys.stderr,
                    )
                    exit_code = code or 1
                    raise SystemExit(exit_code)
            time.sleep(0.4)

    except KeyboardInterrupt:
        print()
        print(_paint("Stopping both services...", "2"))
    except SystemExit:
        pass
    finally:
        for service in services:
            service.stop()
        # The pump threads are daemons reading pipes that just closed; give
        # them a moment so their last lines land before the prompt returns.
        time.sleep(0.2)
        print(_paint("Stopped.", "2"))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
