#!/usr/bin/env python3
# ============================================
# Alcove v1.3.0 — alcove.py
# Pre-flight diagnostics and launcher
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import os
import sys

# ── Auto-activate ~/alcove-env (macOS / Linux only) ─────────────────
# If the venv exists and we're not already inside one, re-execute this
# script with the venv's Python.  This is equivalent to sourcing
# bin/activate — the venv's interpreter automatically uses its own
# site-packages, and VIRTUAL_ENV / PATH are set for any subprocesses.
if os.name != "nt" and not os.environ.get("VIRTUAL_ENV") and sys.prefix == sys.base_prefix:
    _venv_dir = os.path.expanduser("~/alcove-env")
    _venv_python = os.path.join(_venv_dir, "bin", "python3")
    if os.path.isdir(_venv_dir) and os.path.isfile(_venv_python):
        os.environ["VIRTUAL_ENV"] = _venv_dir
        os.environ["PATH"] = os.path.join(_venv_dir, "bin") + os.pathsep + os.environ.get("PATH", "")
        os.execv(_venv_python, [_venv_python] + sys.argv)

import ssl
import subprocess

import importlib

import certifi

config = None  # loaded dynamically after check_config_exists()

LOGO = r"""
    _    _     ____ _____     _______
   / \  | |   / ___/ _ \ \   / / ____|
  / _ \ | |  | |  | | | \ \ / /|  _|
 / ___ \| |__| |__| |_| |\ V / | |___
/_/   \_\_____\____\___/  \_/  |_____|
     Your Companion's Private Space

Version 1.3.0 by Rob   
Copyright (C) 2026
This software may not be used, copied, modified, or distributed without express permission from the author.
"""


def _ok(label):
    print(f"  [ OK ]  {label}")


def _fail(label, detail=""):
    msg = f"  [FAIL]  {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def _warn(label, detail=""):
    msg = f"  [WARN]  {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def check_credentials():
    # Voice-related credentials (ELEVENLABS_*) are intentionally skipped.
    all_good = True
    print("\n-- Credentials --")

    # Discord token is always required
    if getattr(config, "DISCORD_TOKEN", None) and str(config.DISCORD_TOKEN).strip():
        _ok("DISCORD_TOKEN is set")
    else:
        _fail("DISCORD_TOKEN is missing or empty")
        all_good = False

    # Check the API key for the active LLM provider
    prov = getattr(config, "PROVIDER", "openrouter").lower()
    if prov == "nanogpt":
        key_name, key_val = "NANOGPT_KEY", getattr(config, "NANOGPT_KEY", "")
    else:
        key_name, key_val = "OPENROUTER_KEY", getattr(config, "OPENROUTER_KEY", "")

    if key_val and str(key_val).strip():
        _ok(f"{key_name} is set (provider: {prov})")
    else:
        _fail(f"{key_name} is missing or empty (provider: {prov})")
        all_good = False

    return all_good


def check_system_prompt():
    print("\n-- System prompt --")
    path = getattr(config, "SYSTEM_PROMPT_LOCATION", None)
    if not path or str(path).strip() == "":
        _warn("SYSTEM_PROMPT_LOCATION is not set (using built-in default)")
        return True
    if not os.path.isfile(path):
        _fail(f"SYSTEM_PROMPT_LOCATION file not found", path)
        return False
    _ok(f"SYSTEM_PROMPT_LOCATION → {path}")
    return True


def check_optional_models():
    # Optional models: warn (don't fail) if missing. The bot still runs without them.
    print("\n-- Optional models --")
    optional = [
        ("CURRENT_IMAGE_MODEL", "image generation is optional — !image will return an error"),
        ("CURRENT_VOICE_TEXT_MODEL", "voice mode is optional — !voice features will be disabled"),
        ("ELEVENLABS_VOICE_MODEL", "voice mode is optional — !voice features will be disabled"),
    ]
    for name, note in optional:
        value = getattr(config, name, None)
        if value and str(value).strip():
            _ok(f"{name} is set")
        else:
            _warn(f"{name} is missing or empty", note)
    return True


def check_config_exists():
    global config
    print("\n-- Config file --")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.py")
    if os.path.isfile(config_path):
        _ok("config.py found")
        # Ensure the script's directory is on sys.path so `import config`
        # resolves to the local config.py regardless of cwd or how the
        # launcher was invoked (works even if the path contains spaces).
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        config = importlib.import_module("config")
        return True
    else:
        _fail("config.py not found", "copy config_template.py to config.py and update it with your settings")
        return False


def check_file_list(attr_name):
    print(f"\n-- {attr_name} --")
    paths = getattr(config, attr_name, None)
    if paths is None:
        _fail(f"{attr_name} is not defined")
        return False
    if not paths:
        _ok(f"{attr_name} is empty (nothing to check)")
        return True
    all_good = True
    for path in paths:
        if os.path.isfile(path):
            _ok(path)
        else:
            _fail("file not found", path)
            all_good = False
    return all_good


def check_ssl():
    print("\n-- SSL / certificates --")
    ca_path = certifi.where()
    if not os.path.isfile(ca_path):
        _fail("certifi CA bundle not found", ca_path)
        return False
    _ok(f"certifi CA bundle → {ca_path}")

    # Ensure Python's SSL can find the bundle (fixes macOS python.org installer issue)
    os.environ.setdefault("SSL_CERT_FILE", ca_path)
    _ok(f"SSL_CERT_FILE → {os.environ['SSL_CERT_FILE']}")

    # Quick verification: try an actual SSL handshake to discord.com
    try:
        ctx = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
        with ctx.wrap_socket(
            __import__("socket").create_connection(("discord.com", 443), timeout=5),
            server_hostname="discord.com",
        ) as s:
            pass
        _ok("SSL handshake to discord.com succeeded")
    except Exception as e:
        _fail("SSL handshake to discord.com failed", str(e))
        return False

    return True


def main():
    print(LOGO)
    print("\nRunning pre-flight diagnostics...")

    # Show venv status
    print("\n-- Python environment --")
    venv = os.environ.get("VIRTUAL_ENV", "")
    if venv:
        _ok(f"Virtual environment → {venv}")
    else:
        _warn("No virtual environment active")
    _ok(f"Python → {sys.executable}")

    # Config check must come first — all other checks depend on it
    if not check_config_exists():
        print("\nPre-flight checks FAILED. Fix the issues above before launching main.py.")
        sys.exit(1)

    results = [
        check_ssl(),
        check_credentials(),
        check_optional_models(),
        check_system_prompt(),
        check_file_list("INSTRUCTION_LOCATIONS"),
        check_file_list("CONTEXT_HISTORY_LOCATIONS"),
        check_file_list("CONTEXT_REFERENCE_LOCATIONS"),
        check_file_list("LOADED_TOOL_LOCATIONS"),
        check_file_list("SEARCH_REFERENCE_LOCATIONS"),
    ]

    print()
    if not all(results):
        print("Pre-flight checks FAILED. Fix the issues above before launching.")
        sys.exit(1)

    print("All pre-flight checks passed. Launching main.py...\n")
    sys.stdout.flush()
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Run the bot via `python -m modules.main` so relative imports inside
    # the modules/ package work correctly. The CWD is set to the project
    # root so that config.py, companion_data.db, and data directories are
    # found regardless of how alcove.py was invoked.
    # On Windows we avoid os.execv: the MS C runtime re-quotes arguments in a
    # way that mangles paths containing spaces (a long-standing CPython issue),
    # so we use subprocess instead and forward the exit code. On POSIX, execv
    # passes argv as an array directly to the kernel, so spaces are safe.
    if os.name == "nt":
        try:
            completed = subprocess.run(
                [sys.executable, "-u", "-m", "modules.main"],
                cwd=script_dir,
            )
        except KeyboardInterrupt:
            sys.exit(130)
        sys.exit(completed.returncode)
    else:
        os.chdir(script_dir)
        os.execv(sys.executable, [sys.executable, "-u", "-m", "modules.main"])


if __name__ == "__main__":
    main()
