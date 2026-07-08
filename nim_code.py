#!/usr/bin/env python3
"""nim — production CLI for the NVIDIA NIM ↔ Claude Code proxy."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()
err_console = Console(stderr=True)


# ─── Config directory ────────────────────────────────────────────────────────


def config_dir() -> Path:
    base = Path(os.environ.get("APPDIR", Path.home() / ".config"))
    d = base / "nim-proxy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def global_config_path() -> Path:
    return config_dir() / "config.yaml"


def pid_path() -> Path:
    return config_dir() / "nim-proxy.pid"


def log_path() -> Path:
    return config_dir() / "nim-proxy.log"


# ─── Config loading ───────────────────────────────────────────────────────────


def load_env(env_file: Path | None = None) -> None:
    candidates = [env_file] if env_file else [Path(".env"), config_dir() / ".env"]
    for path in candidates:
        if path and path.exists():
            with path.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    val = val.strip().strip("'").strip('"')
                    os.environ.setdefault(key.strip(), val)
            break


def load_config() -> dict:
    for path in [global_config_path(), Path("config.yaml")]:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
    return {}


def save_config(cfg: dict) -> None:
    path = global_config_path()
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True)


def get_proxy_url(config: dict) -> str:
    server = config.get("server", {})
    host = os.environ.get("PROXY_HOST") or server.get("host", "127.0.0.1")
    port = int(os.environ.get("PROXY_PORT") or server.get("port", 8787))
    return f"http://{host}:{port}"


def get_proxy_port(config: dict) -> int:
    server = config.get("server", {})
    return int(os.environ.get("PROXY_PORT") or server.get("port", 8787))


def get_default_model(config: dict) -> str:
    nvidia = config.get("nvidia", {})
    return os.environ.get("DEFAULT_NVIDIA_MODEL") or nvidia.get(
        "default_model", "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    )


def get_tier_models(config: dict) -> tuple[str, str, str]:
    """Return (opus_model, sonnet_model, haiku_model) for Claude Code tier slots.

    Each slot shows a flagship from a different provider so the picker offers
    real variety across NVIDIA's catalog:
      Opus   → deepseek-ai/deepseek-v4-pro   (DeepSeek flagship)
      Sonnet → nvidia/llama-3.3-nemotron-super-49b-v1.5  (NVIDIA default)
      Haiku  → minimaxai/minimax-m2.7         (MiniMax flagship)
    """
    nvidia = config.get("nvidia", {})
    sonnet = get_default_model(config)
    opus = os.environ.get("OPUS_NVIDIA_MODEL") or nvidia.get(
        "opus_model", "deepseek-ai/deepseek-v4-pro"
    )
    haiku = os.environ.get("HAIKU_NVIDIA_MODEL") or nvidia.get(
        "haiku_model", "minimaxai/minimax-m2.7"
    )
    return opus, sonnet, haiku


# ─── PID / process helpers ───────────────────────────────────────────────────


def read_pid() -> int | None:
    p = pid_path()
    if p.exists():
        try:
            return int(p.read_text().strip())
        except Exception:
            pass
    return None


def write_pid(pid: int) -> None:
    pid_path().write_text(str(pid))


def remove_pid() -> None:
    try:
        pid_path().unlink()
    except FileNotFoundError:
        pass


def is_running() -> tuple[bool, int | None]:
    pid = read_pid()
    if pid is None:
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, OSError):
        remove_pid()
        return False, None


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pids_on_port(port: int) -> list[int]:
    """Return process IDs listening on a local TCP port.

    Uses common platform tools instead of adding a runtime dependency. `lsof`
    works on macOS/Linux; `fuser` is a Linux fallback.
    """
    pids: set[int] = set()
    if shutil.which("lsof"):
        try:
            out = subprocess.check_output(
                ["lsof", "-nP", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                if line.strip().isdigit():
                    pids.add(int(line.strip()))
        except subprocess.CalledProcessError:
            pass
    if not pids and shutil.which("fuser"):
        try:
            out = subprocess.check_output(
                ["fuser", f"{port}/tcp"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for part in out.split():
                if part.strip().isdigit():
                    pids.add(int(part.strip()))
        except subprocess.CalledProcessError:
            pass
    return sorted(pids)


def kill_pids(pids: list[int], timeout: float = 3.0) -> tuple[list[int], list[int]]:
    """Terminate PIDs, escalating to SIGKILL after timeout.

    Returns (stopped, failed). Never kills the current CLI process.
    """
    current = os.getpid()
    targets = [pid for pid in pids if pid > 0 and pid != current]
    stopped: list[int] = []
    failed: list[int] = []
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            stopped.append(pid)
        except OSError:
            failed.append(pid)

    deadline = time.time() + timeout
    remaining = [pid for pid in targets if pid not in stopped and pid not in failed]
    while remaining and time.time() < deadline:
        time.sleep(0.1)
        next_remaining: list[int] = []
        for pid in remaining:
            try:
                os.kill(pid, 0)
                next_remaining.append(pid)
            except ProcessLookupError:
                stopped.append(pid)
            except OSError:
                failed.append(pid)
        remaining = next_remaining

    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
            stopped.append(pid)
        except ProcessLookupError:
            stopped.append(pid)
        except OSError:
            failed.append(pid)
    return sorted(set(stopped)), sorted(set(failed))


# ─── Proxy lifecycle ─────────────────────────────────────────────────────────


def wait_for_proxy(url: str, timeout: int = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(trust_env=False) as client:
                if client.get(f"{url}/healthz", timeout=1.0).status_code == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start_daemon(config: dict) -> tuple[bool, str]:
    alive, pid = is_running()
    if alive:
        url = get_proxy_url(config)
        return True, f"already:{pid}:{url}"

    port = get_proxy_port(config)
    url = get_proxy_url(config)
    if is_port_in_use(port):
        # The PID file may be missing/stale while a previous proxy is still
        # healthy on the configured port (common after shell/tmux restarts).
        # Reuse it instead of failing with an unactionable "port in use" error.
        if wait_for_proxy(url, timeout=2):
            return True, f"already:unknown:{url}"
        return False, f"port:{port}"

    proxy_py = Path(__file__).parent / "proxy.py"
    log_file = log_path()
    env = os.environ.copy()

    with log_file.open("a") as lf:
        proc = subprocess.Popen(
            [sys.executable, str(proxy_py)],
            stdout=lf,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

    write_pid(proc.pid)

    if wait_for_proxy(url, timeout=15):
        return True, f"started:{proc.pid}:{url}"

    remove_pid()
    return False, "timeout"


def stop_daemon() -> tuple[bool, str]:
    alive, pid = is_running()
    if not alive:
        return True, "not_running"

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, OSError):
                remove_pid()
                return True, f"stopped:{pid}"
        os.kill(pid, signal.SIGKILL)
        remove_pid()
        return True, f"killed:{pid}"
    except Exception as e:
        return False, str(e)


# ─── Rich UI helpers ──────────────────────────────────────────────────────────

NVIDIA_GREEN = "bright_green"
ACCENT = "cyan"
DIM = "dim white"
WARN = "yellow"
ERR = "red"

NIM_LOGO = (
    "[bold cyan]NIM[/bold cyan] [dim]▸[/dim] [bold white]NVIDIA NIM Proxy[/bold white]"
)


def _pass(label: str, detail: str = "") -> None:
    t = Text()
    t.append("  ✓ ", style="bold green")
    t.append(f"{label:<36}", style="white")
    if detail:
        t.append(detail, style=DIM)
    console.print(t)


def _warn(label: str, detail: str = "") -> None:
    t = Text()
    t.append("  ⚠ ", style="bold yellow")
    t.append(f"{label:<36}", style="yellow")
    if detail:
        t.append(detail, style=DIM)
    console.print(t)


def _fail(label: str, detail: str = "") -> None:
    t = Text()
    t.append("  ✗ ", style="bold red")
    t.append(f"{label:<36}", style="red")
    if detail:
        t.append(detail, style=DIM)
    console.print(t)


def _env_panel(url: str) -> Panel:
    body = Text()
    body.append("  export ", style=DIM)
    body.append("ANTHROPIC_BASE_URL", style="bold cyan")
    body.append("=", style=DIM)
    body.append(url + "\n", style="bright_white")
    body.append("  export ", style=DIM)
    body.append("ANTHROPIC_API_KEY", style="bold cyan")
    body.append("=", style=DIM)
    body.append("not-used", style="bright_white")
    return Panel(
        body,
        title="[dim]Claude Code env vars[/dim]",
        border_style="dim cyan",
        padding=(0, 1),
    )


# ─── Commands ────────────────────────────────────────────────────────────────


def cmd_start(args: argparse.Namespace) -> None:
    config = load_config()
    with console.status("[cyan]Starting proxy…[/cyan]", spinner="dots"):
        ok, msg = start_daemon(config)

    if ok and msg.startswith("already:"):
        _, pid, url = msg.split(":", 2)
        console.print(
            f"\n[yellow]●[/yellow] Already running  [dim]PID {pid}[/dim]  [cyan]{url}[/cyan]\n"
        )
        console.print(_env_panel(url))
        console.print()
    elif ok and msg.startswith("started:"):
        _, pid, url = msg.split(":", 2)
        console.print(
            f"\n[bold green]●[/bold green] Proxy started  [dim]PID {pid}[/dim]  [cyan]{url}[/cyan]\n"
        )
        console.print(_env_panel(url))
        console.print()
    elif not ok and msg.startswith("port:"):
        port = msg.split(":")[1]
        console.print(
            f"\n[red]✗[/red] Port [bold]{port}[/bold] is already in use by another process."
        )
        console.print(
            f"  [dim]Fix:[/dim] run [white]nim kill --port {port}[/white] to stop the blocker, or change [cyan]server.port[/cyan] with [white]nim configure server.port <port>[/white]."
        )
        console.print()
        sys.exit(1)
    elif not ok and msg == "timeout":
        console.print("\n[red]✗[/red] Proxy failed to start within 15 s.")
        console.print(f"  [dim]Check logs:[/dim] [white]nim logs[/white]")
        console.print()
        sys.exit(1)
    else:
        console.print(f"\n[red]✗[/red] {msg}\n")
        sys.exit(1)


def cmd_stop(args: argparse.Namespace) -> None:
    with console.status("[cyan]Stopping proxy…[/cyan]", spinner="dots"):
        ok, msg = stop_daemon()

    if msg == "not_running":
        console.print("\n[dim]● Proxy is not running.[/dim]\n")
    elif ok and msg.startswith("stopped:"):
        pid = msg.split(":")[1]
        console.print(f"\n[green]●[/green] Proxy stopped  [dim]PID {pid}[/dim]\n")
    elif ok and msg.startswith("killed:"):
        pid = msg.split(":")[1]
        console.print(
            f"\n[yellow]●[/yellow] Proxy force-killed  [dim]PID {pid}[/dim]\n"
        )
    else:
        console.print(f"\n[red]✗[/red] {msg}\n")
        sys.exit(1)


def cmd_kill(args: argparse.Namespace) -> None:
    """Kill whichever process is listening on the configured or requested port."""
    config = load_config()
    port = int(getattr(args, "port", None) or get_proxy_port(config))
    pids = pids_on_port(port)
    if not pids:
        console.print(f"\n[dim]● No process is listening on port {port}.[/dim]\n")
        return

    console.print(
        f"\n[yellow]⚠[/yellow] Killing process(es) on port [bold]{port}[/bold]: "
        + ", ".join(str(pid) for pid in pids)
    )
    stopped, failed = kill_pids(pids)
    saved_pid = read_pid()
    if saved_pid and saved_pid in pids:
        remove_pid()

    if stopped:
        console.print(
            "[green]●[/green] Stopped PID(s): " + ", ".join(str(pid) for pid in stopped)
        )
    if failed:
        console.print(
            "[red]✗[/red] Could not stop PID(s): "
            + ", ".join(str(pid) for pid in failed)
        )
        console.print(
            "  [dim]You may need elevated permissions or to stop it manually.[/dim]\n"
        )
        sys.exit(1)
    console.print(f"[green]✓[/green] Port {port} is free.\n")


def cmd_restart(args: argparse.Namespace) -> None:
    console.print()
    with console.status("[cyan]Restarting proxy…[/cyan]", spinner="dots"):
        stop_daemon()
        time.sleep(0.5)
        config = load_config()
        ok, msg = start_daemon(config)

    if ok and (msg.startswith("started:") or msg.startswith("already:")):
        _, pid, url = msg.split(":", 2)
        console.print(
            f"[bold green]●[/bold green] Proxy running  [dim]PID {pid}[/dim]  [cyan]{url}[/cyan]\n"
        )
    else:
        console.print(f"[red]✗[/red] {msg}\n")
        sys.exit(1)


def cmd_logs(args: argparse.Namespace) -> None:
    lp = log_path()
    if not lp.exists():
        console.print(
            "\n[dim]No log file found. Start the proxy first:[/dim] [white]nim start[/white]\n"
        )
        return

    n = getattr(args, "lines", 50) or 50
    follow = getattr(args, "follow", False)

    console.print(f"\n[dim]╸[/dim] [cyan]{lp}[/cyan]\n")

    if follow:
        console.print("[dim]Tailing — Ctrl+C to stop[/dim]\n")
        try:
            with lp.open("r") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        _print_log_line(line.rstrip())
                    else:
                        time.sleep(0.2)
        except KeyboardInterrupt:
            console.print("\n[dim]stopped.[/dim]")
    else:
        lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-n:]:
            _print_log_line(line)
        console.print()


def _print_log_line(line: str) -> None:
    if "ERROR" in line or "error" in line.lower() and "deprecat" not in line.lower():
        console.print(f"[red]{line}[/red]")
    elif "WARNING" in line or "WARN" in line:
        console.print(f"[yellow]{line}[/yellow]")
    elif "startup complete" in line or "started" in line.lower():
        console.print(f"[green]{line}[/green]")
    elif "INFO" in line:
        console.print(f"[dim]{line}[/dim]")
    else:
        console.print(line)


def cmd_status(args: argparse.Namespace) -> None:
    config = load_config()
    url = get_proxy_url(config)
    alive, pid = is_running()

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("key", style="dim", no_wrap=True)
    table.add_column("val", style="white")

    table.add_row("Config", str(global_config_path()))
    table.add_row("URL", f"[cyan]{url}[/cyan]")
    table.add_row("Model", f"[dim]{get_default_model(config)}[/dim]")

    key_set = bool(os.environ.get("NVIDIA_API_KEY"))
    table.add_row(
        "API Key", "[green]● set[/green]" if key_set else "[red]✗ NOT SET[/red]"
    )

    if alive:
        table.add_row(
            "Daemon", f"[bold green]● running[/bold green]  [dim]PID {pid}[/dim]"
        )
        try:
            with httpx.Client(trust_env=False) as client:
                r = client.get(f"{url}/healthz", timeout=2.0)
                health = (
                    "[green]● OK[/green]"
                    if r.status_code == 200
                    else f"[yellow]⚠ {r.status_code}[/yellow]"
                )
        except Exception:
            health = "[red]✗ unreachable[/red]"
        table.add_row("Health", health)
    else:
        table.add_row("Daemon", "[dim]● not running[/dim]")

    console.print(Panel(table, title=NIM_LOGO, border_style="cyan", padding=(0, 1)))


def cmd_init(args: argparse.Namespace) -> None:
    console.print()
    console.print(
        Panel(
            "[bold]Get a FREE key at:[/bold] [cyan underline]https://build.nvidia.com[/cyan underline]",
            title=f"{NIM_LOGO}  [dim]Setup Wizard[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()

    api_key = console.input("[cyan]🔑 NVIDIA_API_KEY[/cyan] › ").strip()
    if not api_key:
        console.print("[red]✗[/red] API key is required.")
        sys.exit(1)

    port = console.input("[cyan]🔌 Port[/cyan] [dim][8787][/dim] › ").strip() or "8787"

    cfg_dir = config_dir()
    env_file = cfg_dir / ".env"
    env_file.write_text(f"NVIDIA_API_KEY={api_key}\nPROXY_PORT={port}\n")

    cfg = load_config()
    cfg.setdefault("server", {})["port"] = int(port)
    save_config(cfg)

    proxy_url = f"http://127.0.0.1:{port}"

    console.print()
    console.print(f"[green]✓[/green] Saved to [dim]{env_file}[/dim]")
    console.print()
    console.print(_env_panel(proxy_url))
    console.print()
    console.print(
        "[dim]Next step:[/dim]  [bold white]nim start[/bold white]   [dim]then[/dim]   [bold white]nim code[/bold white]"
    )
    console.print()


def cmd_doctor(args: argparse.Namespace) -> None:
    import shutil

    console.print()
    console.rule(f"[bold cyan]NIM Proxy Doctor[/bold cyan]", style="cyan")
    console.print()

    # Python version
    v = sys.version_info
    if v >= (3, 9):
        _pass("Python ≥ 3.9", f"{v.major}.{v.minor}.{v.micro}")
    else:
        _fail("Python ≥ 3.9", f"Found {v.major}.{v.minor} — upgrade required")

    # API key
    key = os.environ.get("NVIDIA_API_KEY", "")
    if key:
        _pass("NVIDIA_API_KEY set")
    else:
        _fail("NVIDIA_API_KEY set", f"Add to {config_dir() / '.env'}")

    # NVIDIA reachable
    if key:
        try:
            with httpx.Client() as c:
                r = c.head("https://integrate.api.nvidia.com/v1", timeout=5.0)
                if r.status_code < 500:
                    _pass("NVIDIA API reachable", f"HTTP {r.status_code}")
                else:
                    _fail("NVIDIA API reachable", f"HTTP {r.status_code}")
        except Exception as e:
            _fail("NVIDIA API reachable", str(e)[:60])
    else:
        _warn("NVIDIA API reachable", "skipped — no key")

    # Daemon
    config = load_config()
    alive, pid = is_running()
    url = get_proxy_url(config)
    if alive:
        _pass("Proxy daemon running", f"PID {pid}  {url}")
    else:
        _fail("Proxy daemon running", "nim start")

    if alive:
        try:
            with httpx.Client(trust_env=False) as c:
                r = c.get(f"{url}/healthz", timeout=2.0)
                if r.status_code == 200:
                    _pass("Proxy health endpoint")
                else:
                    _fail("Proxy health endpoint", f"HTTP {r.status_code}")
        except Exception as e:
            _fail("Proxy health endpoint", str(e)[:60])

    # Port
    port = get_proxy_port(config)
    if not alive:
        if is_port_in_use(port):
            _fail("Port available", f"Port {port} in use by another process")
        else:
            _pass("Port available", f"{port} is free")

    # Claude Code
    claude_bin = shutil.which("claude")
    if claude_bin:
        _pass("Claude Code installed", claude_bin)
    else:
        _fail("Claude Code installed", "https://claude.ai/code")

    # Config file
    gcfg = global_config_path()
    if gcfg.exists():
        _pass("Global config exists", str(gcfg))
    else:
        _fail("Global config exists", f"Run: nim init")

    console.print()


def cmd_configure(args: argparse.Namespace) -> None:
    cfg = load_config()

    if getattr(args, "list", False):
        display = copy.deepcopy(cfg)
        if "nvidia" in display and "api_key" in display["nvidia"]:
            display["nvidia"]["api_key"] = "****"

        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        table.add_column("key", style="cyan", no_wrap=True)
        table.add_column("val", style="white")

        def _flatten(d: dict, prefix: str = "") -> None:
            for k, v in d.items():
                full = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
                if isinstance(v, dict):
                    _flatten(v, full)
                else:
                    table.add_row(full, str(v))

        if display:
            _flatten(display)
            console.print(
                Panel(
                    table,
                    title=f"[dim]{global_config_path()}[/dim]",
                    border_style="dim cyan",
                    padding=(0, 1),
                )
            )
        else:
            console.print(f"\n[dim](empty — {global_config_path()})[/dim]\n")
        return

    key_path: str = args.key
    value: str = args.value

    parts = key_path.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})

    coerced: int | bool | str = value
    if value.lower() in ("true", "yes"):
        coerced = True
    elif value.lower() in ("false", "no"):
        coerced = False
    else:
        try:
            coerced = int(value)
        except ValueError:
            pass

    node[parts[-1]] = coerced
    save_config(cfg)
    console.print(
        f"\n[green]✓[/green] [cyan]{key_path}[/cyan] = [white]{coerced!r}[/white]  [dim]({global_config_path()})[/dim]\n"
    )


def cmd_models(args: argparse.Namespace) -> None:
    config = load_config()
    url = get_proxy_url(config)
    alive, _ = is_running()

    if not alive:
        console.print(
            "\n[red]✗[/red] Proxy is not running. Start it first: [white]nim start[/white]\n"
        )
        sys.exit(1)

    with console.status("[cyan]Fetching models…[/cyan]", spinner="dots"):
        try:
            with httpx.Client(trust_env=False) as client:
                resp = client.get(f"{url}/v1/models", timeout=5.0)
        except Exception as e:
            console.print(f"\n[red]✗[/red] Could not connect: {e}\n")
            sys.exit(1)

    if resp.status_code != 200:
        console.print(f"\n[red]✗[/red] Proxy returned {resp.status_code}\n")
        sys.exit(1)

    models = resp.json().get("data", [])
    table = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 2))
    table.add_column("Model ID", style="cyan")
    table.add_column("Type", style="dim")
    for m in sorted(models, key=lambda x: x["id"]):
        table.add_row(m["id"], m.get("object", "model"))

    console.print()
    console.print(
        Panel(
            table,
            title=f"[dim]NVIDIA NIM Models via {url}[/dim]",
            border_style="dim cyan",
            padding=(0, 1),
        )
    )
    console.print()


def cmd_use(args: argparse.Namespace) -> None:
    model: str = args.model.strip()

    # Basic validation: NIM model IDs contain at least one "/"
    if "/" not in model:
        console.print(
            f"\n[red]✗[/red] [bold]{model}[/bold] doesn't look like a NIM model ID."
        )
        console.print("  Expected format: [cyan]<provider>/<model-name>[/cyan]")
        console.print(
            "  Examples: [white]qwen/qwen3-235b-a22b[/white]  [white]meta/llama-3.3-70b-instruct[/white]  [white]z-ai/glm-5.1[/white]\n"
        )
        sys.exit(1)

    cfg = load_config()
    old_model = get_default_model(cfg)
    cfg.setdefault("nvidia", {})["default_model"] = model
    save_config(cfg)

    # If daemon is running, restart so it picks up the new default
    alive, pid = is_running()
    restarted = False
    if alive:
        with console.status(
            "[cyan]Restarting proxy with new model…[/cyan]", spinner="dots"
        ):
            stop_daemon()
            time.sleep(0.4)
            ok, msg = start_daemon(cfg)
        restarted = ok

    console.print()

    body = Text()
    body.append("  Model  ", style="dim")
    body.append(model, style="bold cyan")
    body.append("\n\n", style="")
    body.append(f"  was    ", style="dim")
    body.append(old_model, style="dim white")
    if restarted:
        body.append("\n\n  ", style="")
        body.append("Proxy restarted — new model active immediately.", style="green")
    else:
        body.append("\n\n  ", style="")
        body.append("Run ", style="dim")
        body.append("nim start", style="white")
        body.append(" to apply.", style="dim")

    console.print(
        Panel(
            body,
            title="[bold green]✓ Active model updated[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )
    console.print()

    # Print ready-to-paste one-liner
    console.print("[dim]One-shot override (no config change):[/dim]")
    console.print(f"  [white]nim code --model {model}[/white]\n")
    console.print("[dim]Test the new model now:[/dim]")
    console.print(f"  [white]nim test --model {model}[/white]\n")


def cmd_test(args: argparse.Namespace) -> None:
    config = load_config()
    url = get_proxy_url(config)
    model = getattr(args, "model", None) or get_default_model(config)
    prompt = getattr(args, "prompt", None) or "Say 'proxy OK' in exactly 3 words."

    console.print(
        f"\n[dim]Sending test →[/dim] [cyan]{url}[/cyan]  [dim]model:[/dim] [white]{model}[/white]"
    )

    payload = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": prompt}],
    }

    with console.status("[cyan]Waiting for response…[/cyan]", spinner="dots"):
        try:
            with httpx.Client(trust_env=False) as client:
                resp = client.post(f"{url}/v1/messages", json=payload, timeout=30.0)
        except Exception as e:
            console.print(f"\n[red]✗[/red] {e}\n")
            sys.exit(1)

    if resp.status_code == 200:
        data = resp.json()
        text = next(
            (b["text"] for b in data.get("content", []) if b.get("type") == "text"),
            "(no text block)",
        )
        usage = data.get("usage", {})

        result_text = Text()
        result_text.append(text + "\n\n", style="bold white")
        result_text.append(
            f"status: 200 OK   in: {usage.get('input_tokens', '?')} tok   out: {usage.get('output_tokens', '?')} tok",
            style="dim",
        )
        console.print()
        console.print(
            Panel(
                result_text,
                title="[green]✓ Test passed[/green]",
                border_style="green",
                padding=(1, 2),
            )
        )
        console.print()
    else:
        err_text = resp.text[:300]
        console.print()
        console.print(
            Panel(
                err_text,
                title=f"[red]✗ Error {resp.status_code}[/red]",
                border_style="red",
                padding=(1, 2),
            )
        )
        console.print()
        sys.exit(1)


# ─── Curated flagship model catalogue ────────────────────────────────────────

FLAGSHIP_MODELS: list[dict] = [
    # ── NVIDIA Nemotron (default recommendation) ──
    {
        "p": "NVIDIA",
        "id": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "desc": "best balance · tools · default",
    },
    {
        "p": "NVIDIA",
        "id": "nvidia/llama-3.1-nemotron-ultra-253b-v1",
        "desc": "strongest Nemotron · 253B",
    },
    {
        "p": "NVIDIA",
        "id": "nvidia/llama-3.1-nemotron-70b-instruct",
        "desc": "Nemotron 70B · fast",
    },
    {
        "p": "NVIDIA",
        "id": "nvidia/llama-3.1-nemotron-nano-8b-v1",
        "desc": "Nemotron Nano · ultra-low latency",
    },
    # ── DeepSeek ──
    {
        "p": "DeepSeek",
        "id": "deepseek-ai/deepseek-v4-pro",
        "desc": "flagship · deep reasoning",
    },
    {
        "p": "DeepSeek",
        "id": "deepseek-ai/deepseek-v4-flash",
        "desc": "fast · low latency",
    },
    # ── Qwen ──
    {
        "p": "Qwen",
        "id": "qwen/qwen3.5-397b-a17b",
        "desc": "397B · largest Qwen",
    },
    {
        "p": "Qwen",
        "id": "qwen/qwen3-coder-480b-a35b-instruct",
        "desc": "480B MoE · best coder",
    },
    {
        "p": "Qwen",
        "id": "qwen/qwen3.5-122b-a10b",
        "desc": "128k context · balanced",
    },
    # ── Meta Llama ──
    {
        "p": "Meta",
        "id": "meta/llama-4-maverick-17b-128e-instruct",
        "desc": "vision + tools · Llama 4",
    },
    {
        "p": "Meta",
        "id": "meta/llama-3.3-70b-instruct",
        "desc": "general purpose · 70B",
    },
    {
        "p": "Meta",
        "id": "meta/llama-3.1-8b-instruct",
        "desc": "ultra-fast · edge deployment",
    },
    # ── Mistral ──
    {
        "p": "Mistral",
        "id": "mistralai/mistral-large-3-675b-instruct-2512",
        "desc": "675B · largest Mistral",
    },
    {
        "p": "Mistral",
        "id": "mistralai/mistral-medium-3.5-128b",
        "desc": "balanced · 128B",
    },
    {
        "p": "Mistral",
        "id": "mistralai/mistral-large-2-instruct",
        "desc": "Mistral Large v2 · 131k",
    },
    # ── Google Gemma ──
    {
        "p": "Google",
        "id": "google/gemma-4-31b-it",
        "desc": "Gemma 4 · instruction-tuned",
    },
    {
        "p": "Google",
        "id": "google/gemma-3-12b-it",
        "desc": "Gemma 3 · 12B · mobile",
    },
    # ── Other high-quality providers ──
    {"p": "Z-AI", "id": "z-ai/glm-5.1", "desc": "GLM flagship · Chinese-English"},
    {"p": "Z-AI", "id": "z-ai/glm5", "desc": "latest GLM"},
    {"p": "MiniMax", "id": "minimaxai/minimax-m2.7", "desc": "MiniMax flagship"},
    {"p": "Moonshot", "id": "moonshotai/kimi-k2.6", "desc": "Kimi flagship · 200k"},
    {
        "p": "Microsoft",
        "id": "microsoft/phi-4-multimodal-instruct",
        "desc": "multimodal · efficient",
    },
    {"p": "OpenAI", "id": "openai/gpt-oss-120b", "desc": "GPT OSS 120B"},
    {"p": "ByteDance", "id": "bytedance/seed-oss-36b-instruct", "desc": "Seed OSS"},
    {"p": "StepFun", "id": "stepfun-ai/step-3.5-flash", "desc": "fast · step flash"},
    {
        "p": "Writer",
        "id": "writer/palmyra-creative-122b",
        "desc": "creative writing 122B",
    },
]


def _extract_provider(model_id: str) -> str:
    """Extract provider name from a model ID like 'nvidia/llama-...' -> 'NVIDIA'."""
    if "/" not in model_id:
        return ""
    provider_part = model_id.split("/")[0]
    # Capitalize nicely
    if provider_part.lower() == "nvidia":
        return "NVIDIA"
    if provider_part.lower() == "meta":
        return "Meta"
    if provider_part.lower() == "mistralai":
        return "Mistral"
    if provider_part.lower() == "deepseek-ai":
        return "DeepSeek"
    if provider_part.lower().startswith("microsoft"):
        return "Microsoft"
    if provider_part.lower() == "bytedance":
        return "ByteDance"
    if provider_part.lower().startswith("minimax"):
        return "MiniMax"
    if provider_part.lower().startswith("moonshot"):
        return "Moonshot"
    if provider_part.lower().startswith("openai"):
        return "OpenAI"
    if provider_part.lower().startswith("google"):
        return "Google"
    if provider_part.lower().startswith("qwen"):
        return "Qwen"
    if provider_part.lower().startswith("stepfun"):
        return "StepFun"
    if provider_part.lower().startswith("writer"):
        return "Writer"
    return provider_part.replace("-", " ").title()


def _build_model_display_list(live_models: list[str]) -> list[dict]:
    """Merge live models with FLAGSHIP_MODELS for descriptions.

    Result: each entry has 'provider', 'id', 'desc', 'is_live'.
    Live models that appear in FLAGSHIP_MODELS get their description.
    Live models not in FLAGSHIP_MODELS get an empty or inferred desc.
    Flagship models not available show but are marked [unavailable].
    """
    live_set = set(live_models)
    flagship_by_id = {m["id"]: m for m in FLAGSHIP_MODELS}

    # Start with all live models + all flagship that are live
    seen: set[str] = set()
    display: list[dict] = []

    # First: live models in FLAGSHIP order
    for m in FLAGSHIP_MODELS:
        if m["id"] in live_set:
            display.append({
                "provider": m["p"],
                "id": m["id"],
                "desc": m["desc"],
                "is_live": True,
                "sort_order": len(display),
            })
            seen.add(m["id"])

    # Second: other live models not in FLAGSHIP (with inferred provider)
    for mid in sorted(live_set):
        if mid in seen:
            continue
        provider = _extract_provider(mid)
        display.append({
            "provider": provider,
            "id": mid,
            "desc": "",
            "is_live": True,
            "sort_order": len(display),
        })
        seen.add(mid)

    # Third: FLAGSHIP not currently live (marked unavailable)
    for m in FLAGSHIP_MODELS:
        if m["id"] not in seen:
            display.append({
                "provider": m["p"],
                "id": m["id"],
                "desc": f"{m['desc']}  \[unavailable]",
                "is_live": False,
                "sort_order": len(display),
            })
            seen.add(m["id"])

    return display


def _fetch_available_models(url: str, timeout: float = 5.0) -> list[str]:
    """Fetch model IDs from the proxy's /v1/models endpoint."""
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            resp = client.get(f"{url}/v1/models")
            if resp.status_code == 200:
                data = resp.json()
                models = data.get("data", [])
                return [m["id"] for m in models if m.get("id")]
    except Exception:
        pass
    return []


def pick_model_interactive(url: str, default_model: str) -> str:
    """Fetch live models from the proxy and render an interactive selection list.

    Falls back to static FLAGSHIP_MODELS if the proxy is unreachable.
    """
    # Try to fetch live models from the running proxy
    live_models = _fetch_available_models(url)

    if live_models:
        display = _build_model_display_list(live_models)
        is_live = True
    else:
        # Fallback: just FLAGSHIP_LIST with all marked as "unknown availability"
        display = [
            {
                "provider": m["p"],
                "id": m["id"],
                "desc": m["desc"],
                "is_live": None,
            }
            for m in FLAGSHIP_MODELS
        ]
        is_live = False

    t = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold cyan",
        pad_edge=False,
        show_edge=False,
    )
    t.add_column("#", style="dim", width=3, justify="right")
    t.add_column("Provider", style="bold", min_width=10)
    t.add_column("Model", style="cyan", min_width=44)
    t.add_column("Notes", style="dim")

    prev_provider = ""
    for i, item in enumerate(display, 1):
        provider_cell = item["provider"] if item["provider"] != prev_provider else ""
        note = item["desc"]
        if item["id"] == default_model:
            note += "  [green]← default[/green]"
        if not is_live:
            note += "  [dim](unverified)[/dim]"
        elif not item.get("is_live", True):
            note += "  [red]✗ unused[/red]"
        t.add_row(str(i), provider_cell, item["id"], note)
        prev_provider = item["provider"]

    title = "[bold cyan]Select a model[/bold cyan]"
    if is_live:
        subtitle = f"[dim]{len(display)} available on your NVIDIA account[/dim]"
    else:
        subtitle = "[dim]⚠ proxy offline — showing static catalog[/dim]"

    console.print()
    console.print(
        Panel(
            t,
            title=title,
            subtitle=subtitle,
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print(
        f"[dim]Number, model ID, or Enter for default "
        f"([cyan]{default_model}[/cyan])[/dim]: ",
        end="",
    )
    try:
        raw = input().strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return default_model

    if not raw:
        return default_model
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(display):
            chosen = display[idx]["id"]
            console.print(f"[green]✓[/green] [cyan]{chosen}[/cyan]")
            if is_live:
                status = "live" if display[idx].get("is_live", True) else "static"
                console.print(f"  [dim]({status})[/dim]")
            return chosen
        console.print("[yellow]⚠[/yellow] Invalid number — using default")
        return default_model
    console.print(f"[green]✓[/green] [cyan]{raw}[/cyan]")
    return raw


def cmd_code(args: argparse.Namespace) -> None:
    config = load_config()
    url = get_proxy_url(config)

    # ── Ensure proxy is running BEFORE model picker (so live models are available) ──
    alive, pid = is_running()
    if alive:
        console.print(f"\n[dim]● Reusing proxy[/dim]  PID {pid}  [cyan]{url}[/cyan]")
    else:
        console.print("\n[cyan]●[/cyan] Starting proxy daemon…")
        with console.status(
            "[cyan]Waiting for proxy to be ready…[/cyan]", spinner="dots"
        ):
            ok, msg = start_daemon(config)
        if not ok:
            if msg.startswith("port:"):
                port = msg.split(":")[1]
                console.print(
                    f"[red]✗[/red] Port {port} already in use by a non-proxy process."
                )
                console.print(
                    f"  [dim]Fix:[/dim] run [white]nim kill --port {port}[/white], or run [white]nim configure server.port <free-port>[/white]."
                )
            else:
                console.print(f"[red]✗[/red] {msg}")
            sys.exit(1)
        _, pid, url_final = msg.split(":", 2)
        url = url_final  # use the actual URL from the daemon
        console.print(
            f"[green]●[/green] Proxy ready  [dim]PID {pid}[/dim]  [cyan]{url}[/cyan]"
        )

    # ── Model selection (now proxy is guaranteed running so live models can be fetched) ──
    model = getattr(args, "model", None)
    if not model and sys.stdin.isatty():
        model = pick_model_interactive(url, get_default_model(config))
    if not model:
        model = get_default_model(config)

    console.print()
    console.rule(
        f"[bold cyan]NIM[/bold cyan]  [dim]{model}[/dim]  [dim]→[/dim]  [cyan]{url}[/cyan]",
        style="dim cyan",
    )
    console.print()

    opus_model, sonnet_model, haiku_model = get_tier_models(config)

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = url
    env["ANTHROPIC_API_KEY"] = env.get("ANTHROPIC_API_KEY", "not-used")
    env["ANTHROPIC_MODEL"] = model
    env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku_model
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = opus_model
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = sonnet_model
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = haiku_model
    # Claude Code gateway alignment: expose gateway models, avoid Anthropic-only
    # beta/tool-reference/thinking paths that NVIDIA's OpenAI-compatible API
    # cannot consume, and let the proxy handle any upstream reasoning text.
    env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    env["ENABLE_TOOL_SEARCH"] = "false"
    env["CLAUDE_CODE_DISABLE_THINKING"] = "1"
    env["DISABLE_INTERLEAVED_THINKING"] = "1"

    try:
        subprocess.run(["claude"] + (args.claude_args or []), env=env, check=False)
    except FileNotFoundError:
        console.print(
            "\n[red]✗[/red] [bold]claude[/bold] not found. Install Claude Code: [cyan underline]https://claude.ai/code[/cyan underline]\n"
        )
        sys.exit(1)

    console.print()
    console.rule(
        "[dim]Claude Code session ended — proxy still running[/dim]", style="dim"
    )
    console.print(f"[dim]  Stop with:[/dim] [white]nim stop[/white]\n")


def cmd_proxy(args: argparse.Namespace) -> None:
    from proxy import main as proxy_main

    proxy_main()


def cmd_version(args: argparse.Namespace) -> None:
    try:
        from importlib.metadata import version

        v = version("nim-claude-proxy")
    except Exception:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        p = Path(__file__).parent / "pyproject.toml"
        v = (
            tomllib.loads(p.read_text())["project"]["version"]
            if p.exists()
            else "unknown"
        )
    console.print(f"[bold cyan]nim-claude-proxy[/bold cyan] [white]{v}[/white]")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    load_env()

    parser = argparse.ArgumentParser(
        prog="nim",
        description="NVIDIA NIM ↔ Claude Code proxy — production CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("start", help="Start proxy as background daemon")
    sub.add_parser("stop", help="Stop running daemon")
    kill_p = sub.add_parser("kill", help="Kill the process listening on the proxy port")
    kill_p.add_argument(
        "--port", type=int, help="Port to free (defaults to effective proxy port)"
    )
    sub.add_parser("restart", help="Restart daemon")

    logs_p = sub.add_parser("logs", help="Show proxy logs")
    logs_p.add_argument(
        "-f", "--follow", action="store_true", help="Tail log in real time"
    )
    logs_p.add_argument(
        "-n", "--lines", type=int, default=50, help="Number of lines (default 50)"
    )

    sub.add_parser("status", help="Show proxy status")
    sub.add_parser("init", help="Interactive setup wizard")
    sub.add_parser("doctor", help="Diagnose configuration problems")

    cfg_p = sub.add_parser("configure", help="Set or list config values")
    cfg_p.add_argument(
        "key", nargs="?", help="Dot-notation config key (e.g. server.port)"
    )
    cfg_p.add_argument("value", nargs="?", help="Value to set")
    cfg_p.add_argument("--list", action="store_true", help="Print effective config")

    sub.add_parser(
        "models", help="List available NVIDIA models (proxy must be running)"
    )

    use_p = sub.add_parser(
        "use", help="Switch active model (e.g. nim use qwen/qwen3-235b-a22b)"
    )
    use_p.add_argument(
        "model", help="NIM model ID — any provider/model from build.nvidia.com"
    )

    test_p = sub.add_parser("test", help="Send a test request through the proxy")
    test_p.add_argument("prompt", nargs="?", help="Custom test prompt")
    test_p.add_argument("--model", help="Override model")

    code_p = sub.add_parser(
        "code", help="Start proxy (if needed) then launch Claude Code"
    )
    code_p.add_argument("--model", help="Override model")
    code_p.add_argument("claude_args", nargs="*", help="Extra args forwarded to claude")

    sub.add_parser("proxy", help="Start proxy in foreground (for debugging)")
    sub.add_parser("version", help="Show version")

    args = parser.parse_args()

    dispatch = {
        "use": cmd_use,
        "start": cmd_start,
        "stop": cmd_stop,
        "kill": cmd_kill,
        "restart": cmd_restart,
        "logs": cmd_logs,
        "status": cmd_status,
        "init": cmd_init,
        "doctor": cmd_doctor,
        "configure": cmd_configure,
        "models": cmd_models,
        "test": cmd_test,
        "code": cmd_code,
        "proxy": cmd_proxy,
        "version": cmd_version,
    }

    if args.command in dispatch:
        dispatch[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
