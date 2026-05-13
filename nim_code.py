#!/usr/bin/env python3
"""nim — production CLI for the NVIDIA NIM ↔ Claude Code proxy."""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import json
from pathlib import Path

import httpx
import yaml


# ─── Config directory ────────────────────────────────────────────────────────

def config_dir() -> Path:
    """Return ~/.config/nim-proxy/ (or $APPDIR/nim-proxy/)."""
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
    """Load .env from given path or search CWD then config dir."""
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
    """Search: global config → local config → empty dict."""
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
    return (
        os.environ.get("DEFAULT_NVIDIA_MODEL")
        or nvidia.get("default_model", "nvidia/llama-3.3-nemotron-super-49b-v1.5")
    )


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
    """Return (alive, pid). Uses os.kill(pid, 0) to check without signaling."""
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
    """Start proxy as background daemon. Returns (success, message)."""
    alive, pid = is_running()
    if alive:
        url = get_proxy_url(config)
        return True, f"Already running (PID {pid}) at {url}"

    port = get_proxy_port(config)
    if is_port_in_use(port):
        return False, (
            f"Port {port} is already in use by another process.\n"
            f"  Fix: change port in {global_config_path()} or stop the process using port {port}."
        )

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
    url = get_proxy_url(config)

    if wait_for_proxy(url, timeout=15):
        return True, f"Proxy started (PID {proc.pid}) at {url}"

    remove_pid()
    return False, f"Proxy failed to start within 15s. Check logs: nim logs"


def stop_daemon() -> tuple[bool, str]:
    """Stop running daemon. Returns (success, message)."""
    alive, pid = is_running()
    if not alive:
        return True, "Proxy is not running."

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, OSError):
                remove_pid()
                return True, f"Proxy stopped (PID {pid})."
        # Force kill if still alive after 5s
        os.kill(pid, signal.SIGKILL)
        remove_pid()
        return True, f"Proxy force-killed (PID {pid})."
    except Exception as e:
        return False, f"Error stopping proxy: {e}"


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_start(args: argparse.Namespace) -> None:
    config = load_config()
    ok, msg = start_daemon(config)
    if ok:
        url = get_proxy_url(config)
        print(f"✅ {msg}")
        print(f"\n  Export these vars for Claude Code:")
        print(f"    export ANTHROPIC_BASE_URL={url}")
        print(f"    export ANTHROPIC_API_KEY=not-used")
    else:
        print(f"❌ {msg}")
        sys.exit(1)


def cmd_stop(args: argparse.Namespace) -> None:
    ok, msg = stop_daemon()
    if ok:
        print(f"✅ {msg}")
    else:
        print(f"❌ {msg}")
        sys.exit(1)


def cmd_restart(args: argparse.Namespace) -> None:
    print("🔄 Restarting proxy...")
    stop_daemon()
    time.sleep(0.5)
    config = load_config()
    ok, msg = start_daemon(config)
    if ok:
        print(f"✅ {msg}")
    else:
        print(f"❌ {msg}")
        sys.exit(1)


def cmd_logs(args: argparse.Namespace) -> None:
    lp = log_path()
    if not lp.exists():
        print("No log file found. Start the proxy first with: nim start")
        return

    n = getattr(args, "lines", 50) or 50
    follow = getattr(args, "follow", False)

    if follow:
        print(f"📋 Tailing {lp} (Ctrl+C to stop)...\n")
        try:
            with lp.open("r") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        print(line, end="")
                    else:
                        time.sleep(0.2)
        except KeyboardInterrupt:
            pass
    else:
        lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"📋 Last {n} lines from {lp}:\n")
        for line in lines[-n:]:
            print(line)


def cmd_status(args: argparse.Namespace) -> None:
    config = load_config()
    url = get_proxy_url(config)
    alive, pid = is_running()

    print(f"NIM Proxy Status")
    print("─" * 50)
    print(f"  Config      : {global_config_path()}")
    print(f"  URL         : {url}")
    print(f"  Model       : {get_default_model(config)}")
    print(f"  API key     : {'✅ set' if os.environ.get('NVIDIA_API_KEY') else '❌ NOT SET'}")

    if alive:
        print(f"  Daemon      : ✅ running (PID {pid})")
        try:
            with httpx.Client(trust_env=False) as client:
                r = client.get(f"{url}/healthz", timeout=2.0)
                print(f"  Health      : {'✅ OK' if r.status_code == 200 else f'⚠️ {r.status_code}'}")
        except Exception as e:
            print(f"  Health      : ❌ unreachable ({e})")
    else:
        print(f"  Daemon      : ❌ not running")


def cmd_init(args: argparse.Namespace) -> None:
    print("\n" + "=" * 60)
    print("🛠️  NVIDIA NIM Proxy — Setup Wizard")
    print("=" * 60 + "\n")

    print("Get a FREE NVIDIA API key at: https://build.nvidia.com\n")
    api_key = input("🔑 Enter your NVIDIA_API_KEY: ").strip()
    if not api_key:
        print("❌ API key is required.")
        sys.exit(1)

    port = input("🔌 Proxy port [8787]: ").strip() or "8787"

    cfg_dir = config_dir()
    env_file = cfg_dir / ".env"
    env_file.write_text(f"NVIDIA_API_KEY={api_key}\nPROXY_PORT={port}\n")

    cfg = load_config()
    cfg.setdefault("server", {})["port"] = int(port)
    save_config(cfg)

    proxy_url = f"http://127.0.0.1:{port}"

    print(f"\n✅ Saved to {env_file}")
    print(f"\nNext steps:")
    print(f"  nim start")
    print(f"\nThen for Claude Code, export:")
    print(f"  export ANTHROPIC_BASE_URL={proxy_url}")
    print(f"  export ANTHROPIC_API_KEY=not-used")
    print(f"\nOr just run:  nim code")


def cmd_doctor(args: argparse.Namespace) -> None:
    import shutil

    print("\n🩺 NIM Proxy Doctor\n")

    def check(label: str, ok: bool, detail: str = "") -> None:
        icon = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {icon}  {label}")
        if detail:
            print(f"         {detail}")

    # Python version
    v = sys.version_info
    check("Python ≥ 3.9", v >= (3, 9), f"Found {v.major}.{v.minor}.{v.micro}")

    # API key
    key = os.environ.get("NVIDIA_API_KEY", "")
    check("NVIDIA_API_KEY set", bool(key), "" if key else f"Set it in {config_dir() / '.env'}")

    # NVIDIA API reachable
    if key:
        try:
            with httpx.Client() as c:
                r = c.head("https://integrate.api.nvidia.com/v1", timeout=5.0)
                check("NVIDIA API reachable", r.status_code < 500, f"HTTP {r.status_code}")
        except Exception as e:
            check("NVIDIA API reachable", False, str(e)[:80])
    else:
        print("  ⚠️ WARN  NVIDIA API reachable  (skipped — no key)")

    # Proxy
    config = load_config()
    alive, pid = is_running()
    url = get_proxy_url(config)
    check("Proxy daemon running", alive, f"PID {pid} at {url}" if alive else f"Run: nim start")

    if alive:
        try:
            with httpx.Client(trust_env=False) as c:
                r = c.get(f"{url}/healthz", timeout=2.0)
                check("Proxy health endpoint", r.status_code == 200)
        except Exception as e:
            check("Proxy health endpoint", False, str(e)[:80])

    # Port
    port = get_proxy_port(config)
    if not alive:
        in_use = is_port_in_use(port)
        if in_use:
            check("Port available", False, f"Port {port} is in use by another process")
        else:
            check("Port available", True, f"Port {port} is free")

    # Claude Code
    claude_bin = shutil.which("claude")
    check("Claude Code installed", bool(claude_bin), claude_bin or "Install from https://claude.ai/code")

    # Config file
    gcfg = global_config_path()
    check("Global config exists", gcfg.exists(), str(gcfg))

    print()


def cmd_configure(args: argparse.Namespace) -> None:
    cfg = load_config()

    if getattr(args, "list", False):
        import copy
        display = copy.deepcopy(cfg)
        # Redact secrets
        if "nvidia" in display and "api_key" in display["nvidia"]:
            display["nvidia"]["api_key"] = "****"
        print(f"Effective config ({global_config_path()}):\n")
        print(yaml.dump(display, default_flow_style=False) if display else "  (empty)")
        return

    key_path: str = args.key
    value: str = args.value

    # Set dot-notation key, e.g. "server.port" or "nvidia.default_model"
    parts = key_path.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})

    # Try to coerce value to int/bool
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
    print(f"✅ Set {key_path} = {coerced!r} in {global_config_path()}")


def cmd_models(args: argparse.Namespace) -> None:
    config = load_config()
    url = get_proxy_url(config)
    alive, _ = is_running()

    if not alive:
        print("❌ Proxy is not running. Start it first: nim start")
        sys.exit(1)

    try:
        with httpx.Client(trust_env=False) as client:
            resp = client.get(f"{url}/v1/models", timeout=5.0)
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                print(f"\nAvailable NVIDIA NIM Models ({url}):")
                print("─" * 60)
                for m in sorted(models, key=lambda x: x["id"]):
                    print(f"  • {m['id']}")
                print("─" * 60)
            else:
                print(f"❌ Error: proxy returned {resp.status_code}")
    except Exception as e:
        print(f"❌ Could not connect to proxy: {e}")
        sys.exit(1)


def cmd_test(args: argparse.Namespace) -> None:
    config = load_config()
    url = get_proxy_url(config)
    model = getattr(args, "model", None) or get_default_model(config)
    prompt = getattr(args, "prompt", None) or "Say 'proxy OK' in exactly 3 words."

    print(f"🧪 Sending test message → {url}  (model: {model})")
    payload = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        with httpx.Client(trust_env=False) as client:
            resp = client.post(f"{url}/v1/messages", json=payload, timeout=30.0)
            if resp.status_code == 200:
                data = resp.json()
                text = next(
                    (b["text"] for b in data.get("content", []) if b.get("type") == "text"),
                    "(no text block)",
                )
                print("\n" + "─" * 60)
                print(f"Result : {text}")
                print(f"Status : ✅ 200 OK")
                print(f"Usage  : {data.get('usage', {})}")
                print("─" * 60 + "\n")
            else:
                print(f"❌ Error {resp.status_code}: {resp.text[:200]}")
                sys.exit(1)
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)


def cmd_code(args: argparse.Namespace) -> None:
    config = load_config()
    url = get_proxy_url(config)
    model = getattr(args, "model", None) or get_default_model(config)

    alive, pid = is_running()
    if alive:
        print(f"📡 Using existing proxy (PID {pid}) at {url}")
    else:
        print("📡 Starting proxy daemon...")
        ok, msg = start_daemon(config)
        if not ok:
            print(f"❌ {msg}")
            sys.exit(1)
        print(f"✅ {msg}")

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = url
    env["ANTHROPIC_API_KEY"] = env.get("ANTHROPIC_API_KEY", "not-used")
    env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = model
    env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"

    print(f"\n{'='*60}")
    print(f"🚀 NIM PROXY → {url}")
    print(f"🤖 MODEL     → {model}")
    print(f"{'='*60}\n")

    try:
        subprocess.run(["claude"] + (args.claude_args or []), env=env, check=False)
    except FileNotFoundError:
        print("❌ 'claude' not found. Install Claude Code first: https://claude.ai/code")
        sys.exit(1)
    # Proxy keeps running after Claude exits — stop with: nim stop


def cmd_proxy(args: argparse.Namespace) -> None:
    """Start proxy in foreground (for debugging/direct use)."""
    from proxy import main as proxy_main
    proxy_main()


def cmd_version(args: argparse.Namespace) -> None:
    try:
        from importlib.metadata import version
        v = version("nvd-claude-nim")
    except Exception:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        p = Path(__file__).parent / "pyproject.toml"
        v = tomllib.loads(p.read_text())["project"]["version"] if p.exists() else "unknown"
    print(f"nvd-claude-nim {v}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    load_env()

    parser = argparse.ArgumentParser(
        prog="nim",
        description="NVIDIA NIM ↔ Claude Code proxy — production CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # nim start
    sub.add_parser("start", help="Start proxy as background daemon")

    # nim stop
    sub.add_parser("stop", help="Stop running daemon")

    # nim restart
    sub.add_parser("restart", help="Restart daemon")

    # nim logs
    logs_p = sub.add_parser("logs", help="Show proxy logs")
    logs_p.add_argument("-f", "--follow", action="store_true", help="Tail log in real time")
    logs_p.add_argument("-n", "--lines", type=int, default=50, help="Number of lines (default 50)")

    # nim status
    sub.add_parser("status", help="Show proxy status")

    # nim init
    sub.add_parser("init", help="Interactive setup wizard")

    # nim doctor
    sub.add_parser("doctor", help="Diagnose configuration problems")

    # nim configure
    cfg_p = sub.add_parser("configure", help="Set or list config values")
    cfg_p.add_argument("key", nargs="?", help="Dot-notation config key (e.g. server.port)")
    cfg_p.add_argument("value", nargs="?", help="Value to set")
    cfg_p.add_argument("--list", action="store_true", help="Print effective config")

    # nim models
    sub.add_parser("models", help="List available NVIDIA models (proxy must be running)")

    # nim test
    test_p = sub.add_parser("test", help="Send a test request through the proxy")
    test_p.add_argument("prompt", nargs="?", help="Custom test prompt")
    test_p.add_argument("--model", help="Override model")

    # nim code
    code_p = sub.add_parser("code", help="Start proxy (if needed) then launch Claude Code")
    code_p.add_argument("--model", help="Override model")
    code_p.add_argument("claude_args", nargs="*", help="Extra args forwarded to claude")

    # nim proxy
    sub.add_parser("proxy", help="Start proxy in foreground (for debugging)")

    # nim version
    sub.add_parser("version", help="Show version")

    args = parser.parse_args()

    dispatch = {
        "start": cmd_start,
        "stop": cmd_stop,
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
