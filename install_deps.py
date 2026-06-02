#!/usr/bin/env python3
# ============================================
# Alcove — install_deps.py
# Check and install Python dependencies
# ============================================
#
# Usage:
#   python3 install_deps.py          (Mac / Linux)
#   python  install_deps.py          (Windows)

import importlib
import importlib.metadata as _ilm
import os
import platform
import re
import subprocess
import sys

# ── Auto-activate ~/alcove-env (macOS / Linux only) ─────────────────
if os.name != "nt" and not os.environ.get("VIRTUAL_ENV") and sys.prefix == sys.base_prefix:
    _venv_dir = os.path.expanduser("~/alcove-env")
    _venv_python = os.path.join(_venv_dir, "bin", "python3")
    if os.path.isdir(_venv_dir) and os.path.isfile(_venv_python):
        os.environ["VIRTUAL_ENV"] = _venv_dir
        os.environ["PATH"] = os.path.join(_venv_dir, "bin") + os.pathsep + os.environ.get("PATH", "")
        os.execv(_venv_python, [_venv_python] + sys.argv)


# ============================================================================
# !!! LIVE VOICE STATUS — shelved. DO NOT pin discord.py to 2.6.4 again. !!!
# ----------------------------------------------------------------------------
# We attempted to support hands-free live voice via `discord-ext-voice-recv`
# but ran into a CLOSED BOX on the current Discord voice protocol — there is
# no version pair of discord.py + voice-recv that works today. The history
# below is here so neither human nor AI assistant repeats the experiment.
#
# What was tried, in order:
#
#   1. discord.py 2.7.1 + latest discord-ext-voice-recv  (initial attempt)
#      → Voice gateway connects fine. The PacketRouter thread inside
#        voice-recv throws `discord.opus.OpusError: corrupted stream` on the
#        FIRST audio frame from the speaker, which kills the decoder thread
#        before any PCM reaches our sink. Result: live voice never sees
#        audio → never finalizes an utterance → never calls STT/LLM/TTS.
#        Upstream-confirmed issue:
#            https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues/53
#        ("Voice recv receives opus packets that decode to gibberish audio
#        after upgrading to discord.py v2.7.1 from v2.6.4")
#        At the time of writing, the issue is open and unresolved.
#
#   2. Pinned discord.py to ==2.6.4 (the last version voice-recv worked
#      against) and added force-reinstall logic to actively downgrade
#      existing 2.7.x installs.
#      → Outcome was WORSE than #1: even normal `!join` push-to-talk stops
#        working. The voice gateway closes the handshake immediately with
#        WebSocket close code **4017** (unsupported encryption mode).
#
#        Why: Discord rolled out DAVE (their end-to-end-encrypted voice
#        protocol) through 2024–2025 and progressively retired the older
#        encryption modes. discord.py 2.7.x added support for the new
#        modes; 2.6.4 predates that work and only knows the retired ones.
#        Discord's servers now reject 2.6.4 outright. This is a hard wall
#        on Discord's side — no client-side workaround exists.
#
#   3. (rollback) Removed the pin. discord.py is left UNPINNED so users
#      always run a version that can at least connect to the voice gateway
#      for `!join` push-to-talk and TTS playback. This is the current state.
#
# Lesson — DO NOT re-pin discord.py to a pre-2.7 version. Whatever live-voice
# breakage exists today, downgrading discord.py will make BOTH live voice
# AND regular voice unusable, because Discord's servers no longer accept
# the encryption modes the older client speaks.
#
# Live voice (`!joinLive`) is therefore shelved until one of:
#   (a) discord-ext-voice-recv updates to be compatible with discord.py 2.7+
#       (track issue #53 above — most likely path),
#   (b) we migrate the `livevoice.py` module to py-cord running in a
#       separate sidecar process — py-cord has its own working voice-recv
#       and supports the new encryption modes, but cannot coexist with
#       discord.py in the same Python interpreter (namespace collision on
#       `import discord`), so it would have to be an out-of-process daemon,
#   (c) we implement voice receive ourselves on top of discord.py 2.7's
#       voice client (RTP demux + opus decode + SSRC→user mapping +
#       integration with the new encryption modes — significant work).
#
# The `livevoice.py` module and the `!joinLive` command are still in the
# codebase but inert: with the optional deps (`discord-ext-voice-recv`,
# `webrtcvad-wheels`) uninstalled, dependencies_available() returns False
# and `!joinLive` surfaces a clean "missing optional deps" message.
#
# The version-aware install logic below (parse_pin / force-reinstall on
# version mismatch) is retained for future use — harmless when no `==`
# spec is present, and useful when a different package needs pinning later.
# ============================================================================


# Mapping: (pip package name [optionally with version spec], import name,
#           description, required)
# - "required" means the bot won't start without it; optional means it
#   will run but with reduced functionality.
# - If the pip name contains an `==X.Y.Z` version spec, install_deps will
#   verify the *installed* version matches and force-reinstall if not
#   (downgrade or upgrade as needed). Specs without a version pin (e.g.
#   "aiohttp") are only installed when the package is missing entirely.
DEPENDENCIES = [
    ("discord.py[voice]", "discord", "Discord bot framework (with voice support)", True),
    ("aiohttp",      "aiohttp",      "Async HTTP client",                   True),
    ("certifi",      "certifi",      "SSL certificate bundle",              True),
    ("trafilatura",  "trafilatura",   "Web page content extraction",         True),
    ("pydub",        "pydub",         "Audio processing",                    True),
    ("elevenlabs",   "elevenlabs",    "ElevenLabs TTS/STT (voice features)", False),
    ("PyNaCl",       "nacl",          "Voice encryption (discord.py voice)",  False),
    ("discord-ext-voice-recv", "discord.ext.voice_recv", "Voice receiving (live voice mode)", False),
    ("chromadb", "chromadb", "Vector database (semantic search)", True),
]


# Regex to peel an `==X.Y.Z` style version spec off a pip name like
# "discord.py[voice]==2.6.4". We ONLY honor `==` (exact pins); other
# operators (>=, <, ~=) are ignored by the version-mismatch check, which
# matches how pip itself treats range specs (no forced reinstall).
_PIN_RE = re.compile(r"^(?P<base>[^=<>!~ ]+(?:\[[^\]]+\])?)\s*==\s*(?P<ver>[^\s,]+)\s*$")


def parse_pin(pip_name):
    """Split a pip name into (base, version) when an exact `==` pin is present.

    Returns (base, version) on a hit, or (pip_name, None) when no pin is
    specified or the spec uses a non-`==` operator.

    The base preserves any extras suffix, e.g. "discord.py[voice]==2.6.4"
    splits to ("discord.py[voice]", "2.6.4"). The extras are kept on the
    base so the canonical-name lookup below still works (we strip them at
    that step).
    """
    m = _PIN_RE.match(pip_name)
    if not m:
        return pip_name, None
    return m.group("base"), m.group("ver")


def get_installed_version(pip_base):
    """Return the installed version string for a pip distribution, or None.

    `pip_base` may include extras (e.g. "discord.py[voice]"); we strip
    those for the metadata lookup since extras aren't part of the
    distribution name.
    """
    name = pip_base.split("[", 1)[0]
    try:
        return _ilm.version(name)
    except _ilm.PackageNotFoundError:
        return None
    except Exception:
        return None


def check_installed(import_name):
    """Return True if the package can be imported."""
    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        return False


def install_package(pip_name, force_reinstall=False):
    """Attempt to pip install a package. Returns (success, output).

    `pip_name` may include a version spec (e.g. "discord.py[voice]==2.6.4");
    pip honors it natively. `force_reinstall=True` adds `--force-reinstall`
    so an already-installed wrong-version package is replaced.
    """
    cmd = [sys.executable, "-m", "pip", "install"]
    if force_reinstall:
        cmd.append("--force-reinstall")
    cmd.append(pip_name)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "Installation timed out (180s)"
    except Exception as e:
        return False, str(e)


def preload_chromadb_embeddings():
    """Force-download ChromaDB's default embedding model.

    ChromaDB lazily downloads its ONNX embedding model on first use. This
    can fail at runtime (file-locking, permission, or network issues during
    bot startup), so we pre-download it here as a separate step after the
    pip package is confirmed installed.
    """
    print("  Pre-downloading ChromaDB embedding model...")
    cmd = [
        sys.executable, "-c",
        "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; "
        "ef = DefaultEmbeddingFunction(); "
        "ef(['force download'])",
    ]
    try:
        result = subprocess.run(cmd)
        if result.returncode == 0:
            print("  ✅ Embedding model downloaded")
            return True, None
        else:
            print("  ⚠️  pre-download failed (bot may still work)")
            return False, "non-zero exit"
    except Exception as e:
        print(f"  ⚠️  pre-download error: {e}")
        return False, str(e)


def main():
    print()
    print("=" * 50)
    print("  Alcove Dependency Installer")
    print("=" * 50)
    print()
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  Path:   {sys.executable}")
    print()

    already_installed = []
    to_install = []           # missing entirely
    to_correct_version = []   # installed but wrong pinned version
    results = []

    # Phase 1: Check what's installed (and at the correct version, when pinned)
    print("── Checking installed packages ──")
    print()
    for pip_name, import_name, description, required in DEPENDENCIES:
        tag = "required" if required else "optional"
        pip_base, pinned_ver = parse_pin(pip_name)

        if not check_installed(import_name):
            print(f"  ❌ {pip_name:<28} {description} [{tag}]")
            to_install.append((pip_name, import_name, description, required))
            continue

        # Importable. If a version is pinned, verify it matches.
        if pinned_ver is not None:
            installed_ver = get_installed_version(pip_base)
            if installed_ver is None:
                # Importable but pip metadata can't find it — odd, but treat
                # as "needs reinstall" to be safe.
                print(f"  ⚠️  {pip_name:<28} {description} (version unverifiable — will reinstall)")
                to_correct_version.append(
                    (pip_name, import_name, description, required, "unknown", pinned_ver)
                )
                continue
            if installed_ver != pinned_ver:
                print(
                    f"  ⚠️  {pip_name:<28} {description} "
                    f"(installed {installed_ver}, need {pinned_ver} — will downgrade/upgrade)"
                )
                to_correct_version.append(
                    (pip_name, import_name, description, required, installed_ver, pinned_ver)
                )
                continue
            # Match — fall through to "already installed".
            print(f"  ✅ {pip_name:<28} {description} (== {installed_ver})")
        else:
            print(f"  ✅ {pip_name:<28} {description}")
        already_installed.append(pip_name)

    nothing_to_do = not to_install and not to_correct_version
    if nothing_to_do:
        print()
        print("  All dependencies are already installed at the correct versions!")
        # Still run post-install steps before returning.
        if check_installed("chromadb"):
            print()
            print("── Pre-downloading ChromaDB embedding model ──")
            print()
            preload_chromadb_embeddings()
        print()
        return 0

    # Phase 2a: Force-reinstall mismatched pinned versions FIRST.
    # Doing these before fresh installs means a downgrade of e.g. discord.py
    # is in place before any package that depends on it gets touched.
    failures = []
    if to_correct_version:
        print()
        print(f"── Correcting {len(to_correct_version)} pinned version(s) ──")
        print()
        for entry in to_correct_version:
            pip_name, import_name, description, required, old_ver, new_ver = entry
            print(f"  {old_ver} → {new_ver}: {pip_name}... ", end="", flush=True)
            success, output = install_package(pip_name, force_reinstall=True)
            if success and check_installed(import_name):
                # Confirm we actually moved to the pinned version.
                pip_base, _ = parse_pin(pip_name)
                actual = get_installed_version(pip_base)
                if actual == new_ver:
                    print("✅")
                    results.append((pip_name, True, None))
                else:
                    print(f"⚠️  installed but version is {actual}, expected {new_ver}")
                    results.append((pip_name, False, f"version mismatch after install: {actual}"))
                    failures.append((pip_name, required))
            else:
                print("❌")
                first_line = (output or "").split("\n")[-1] if output else "Unknown error"
                print(f"     {first_line[:80]}")
                results.append((pip_name, False, output))
                failures.append((pip_name, required))

    # Phase 2b: Fresh-install missing packages.
    if to_install:
        print()
        print(f"── Installing {len(to_install)} missing package(s) ──")
        print()
        for pip_name, import_name, description, required in to_install:
            print(f"  Installing {pip_name}... ", end="", flush=True)
            success, output = install_package(pip_name)
            if success:
                if check_installed(import_name):
                    print("✅")
                    results.append((pip_name, True, None))
                else:
                    print("⚠️  installed but import failed")
                    results.append((pip_name, False, "Package installed but cannot be imported"))
                    failures.append((pip_name, required))
            else:
                print("❌")
                first_line = (output or "").split("\n")[-1] if output else "Unknown error"
                print(f"           {first_line[:80]}")
                results.append((pip_name, False, output))
                failures.append((pip_name, required))

    # Phase 2c: Pre-download ChromaDB embedding model.
    # Runs after chromadb has been installed/corrected above, or if it was
    # already present but other packages needed work.
    if check_installed("chromadb"):
        print()
        print("── Pre-downloading ChromaDB embedding model ──")
        print()
        preload_chromadb_embeddings()

    # Phase 3: Summary
    print()
    print("=" * 50)
    print("  Summary")
    print("=" * 50)
    print()
    correction_names = {e[0] for e in to_correct_version}
    install_names = {e[0] for e in to_install}
    print(f"  Already correct:    {len(already_installed)}")
    print(f"  Version-corrected:  {sum(1 for pn, ok, _ in results if ok and pn in correction_names)}")
    print(f"  Newly installed:    {sum(1 for pn, ok, _ in results if ok and pn in install_names)}")
    if failures:
        print(f"  Failed:             {len(failures)}")
        print()
        required_failures = [name for name, req in failures if req]
        optional_failures = [name for name, req in failures if not req]
        if required_failures:
            print(f"  ❌ REQUIRED packages that failed to install:")
            for name in required_failures:
                print(f"       - {name}")
            print()
            print("     The bot will NOT start without these.")
            print("     Try installing manually:")
            print(f"       {sys.executable} -m pip install {' '.join(required_failures)}")
        if optional_failures:
            print(f"  ⚠️  Optional packages that failed to install:")
            for name in optional_failures:
                print(f"       - {name}")
            print()
            print("     The bot will still run but some features (e.g. voice)")
            print("     may not work without these.")
    else:
        print()
        print("  ✅ All dependencies are ready!")

    print()
    return 1 if any(req for _, req in failures) else 0


if __name__ == "__main__":
    sys.exit(main())
