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

# ── Auto-activate ~/alcove-env (macOS / Linux / Windows) ────────────
if not os.environ.get("VIRTUAL_ENV") and sys.prefix == sys.base_prefix:
    _venv_dir = os.path.expanduser("~/alcove-env")
    if os.name == "nt":
        _venv_bin = os.path.join(_venv_dir, "Scripts")
        _venv_python = os.path.join(_venv_bin, "python.exe")
    else:
        _venv_bin = os.path.join(_venv_dir, "bin")
        _venv_python = os.path.join(_venv_bin, "python3")
    if os.path.isfile(_venv_python):
        os.environ["VIRTUAL_ENV"] = _venv_dir
        os.environ["PATH"] = _venv_bin + os.pathsep + os.environ.get("PATH", "")
        if os.name == "nt":
            # Windows os.execv is not a true exec (it spawns a new process
            # and mangles arguments containing spaces), so launch a
            # subprocess instead and forward its exit code.
            import subprocess
            try:
                completed = subprocess.run([_venv_python] + sys.argv)
            except KeyboardInterrupt:
                sys.exit(130)
            sys.exit(completed.returncode)
        os.execv(_venv_python, [_venv_python] + sys.argv)


# ============================================================================
# LIVE VOICE STATUS — receiving is shelved until pycord fixes DAVE receive.
# ----------------------------------------------------------------------------
# Alcove uses pycord (py-cord) as its Discord library. Pycord 2.8+ has native
# voice receiving via Sink / start_listening(), which replaces the old
# discord-ext-voice-recv third-party extension we previously used with
# discord.py. However, Discord's DAVE (End-to-End Encryption) protocol for
# voice calls breaks the receive path: pycord's start_listening() currently
# emits a RuntimeWarning that voice reception is broken due to DAVE.
# Tracked at: https://github.com/Pycord-Development/pycord/issues/3139
#
# History (pre-migration to pycord):
#
#   1. discord.py 2.7.1 + latest discord-ext-voice-recv  (initial attempt)
#      → Voice gateway connects fine. The PacketRouter thread inside
#        voice-recv throws `discord.opus.OpusError: corrupted stream` on the
#        FIRST audio frame from the speaker, which kills the decoder thread
#        before any PCM reaches our sink. Result: live voice never sees
#        audio → never finalizes an utterance → never calls STT/LLM/TTS.
#        Upstream-confirmed issue:
#            https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues/53
#        At the time of writing, the issue is open and unresolved.
#
#   2. Pinned discord.py to ==2.6.4 (the last version voice-recv worked
#      against) and added force-reinstall logic to actively downgrade
#      existing 2.7.x installs.
#      → Outcome was WORSE than #1: even normal `!join` push-to-talk stops
#        working. The voice gateway closes the handshake immediately with
#        WebSocket close code **4017** (unsupported encryption mode).
#
#        Why: Discord rolled out DAVE through 2024–2025 and progressively
#        retired the older encryption modes. discord.py 2.7.x added support
#        for the new modes; 2.6.4 predates that work and only knows the
#        retired ones. Discord's servers now reject 2.6.4 outright.
#
#   3. (rollback) Removed the pin. discord.py was left UNPINNED so users
#      always run a version that can at least connect to the voice gateway
#      for `!join` push-to-talk and TTS playback.
#
#   4. Migrated from discord.py to pycord 2.8+. Pycord supports DAVE
#      encryption for SENDING (connect/play/disconnect), so `!join`
#      push-to-talk works. Voice RECEIVING is still broken due to DAVE —
#      pycord issue #3139 is in progress. The livevoice.py module has been
#      rewritten to use pycord's native Sink API, so when the pycord team
#      ships a DAVE-compatible receive pipeline, !joinLive should work
#      without further code changes on our side.
#
# The `livevoice.py` module and the `!joinLive` command are still in the
# codebase but inert: the pycord DAVE receive fix is not yet available, so
# start_listening() will emit a warning and voice reception will not work.
# When pycord #3139 is resolved, live voice should become functional.
#
# The version-aware install logic below (parse_pin / force-reinstall on
# version mismatch) is retained for future use — harmless when no `==`
# spec is present, and useful when a different package needs pinning later.
#
# TEMPORARY (until pycord ships a release containing PR #3159 — the
# `fix/voice-rec-2` branch, which adds DAVE voice-receive support):
# install_deps.py auto-installs the PR build from its git commit when the
# fix is not yet present in the installed pycord. Detection uses the
# lowercase `has_davey` marker that PR #3159 introduced in
# discord.voice.utils.dependencies, so the hack self-disables once a
# pycord release contains the fix (it will NOT backrev a newer fixed
# release). See _PYCORD_PR_SPEC / _ensure_pycord_fix below. Revert those
# and the Phase 0.5 call once pycord publishes the fix.
#
# Pinned commit: 326b72a (Jul 22, 2026). This build has been running in
# production here for months and has a positive field report upstream
# (https://github.com/Pycord-Development/pycord/pull/3159#issuecomment-5060105213
# — rock-solid on iOS/macOS for multi-user 30-min sessions). Newer commits
# on the branch (through f53d394, Sep 2) are only master-merge syncs and
# pre-commit style fixes — no functional voice-receive changes — so the
# pin is intentionally NOT bumped. The PR also has one outstanding
# requested-changes review (vmphase, Jul 26) for a `stop_recording()`
# callback bug when the callback takes no args (`self.after and self.args`
# should be `is not None` checks); that fix is NOT in 326b72a and is not
# worth a pin bump until it lands.
#
# Release ETA: pycord adopted a formal quadrimestrial release schedule
# (RELEASE_SCHEDULE.md, PR #3097): 4-month cycles, feature freeze ≥1 week
# before each RC. PR #3159 is in the 2.9.0rc1 milestone, which was
# nominally due Sep 4, 2026 but at ~10% completion will slip into Q3
# (Oct 2026). Realistic estimates: 2.9.0rc1 ~Oct 2026, 2.9.0 final
# ~Nov 2026. When 2.9.0rc1 ships, the `has_davey` self-detection will
# switch us to the real release automatically; then delete the
# _PYCORD_PR_SPEC / _ensure_pycord_fix block and the Phase 0.5 call.
#
# NOTE: The git+https:// URL requires git to be installed and on PATH.
# On Windows, install Git for Windows (https://git-scm.com/download/win)
# before running this installer.
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
    ("py-cord[voice]", "discord", "Discord bot framework — pycord (with voice support)", True),
    ("aiohttp",      "aiohttp",      "Async HTTP client",                   True),
    ("curl_cffi",    "curl_cffi",    "Browser-impersonating HTTP client (readweb)", True),
    ("certifi",      "certifi",      "SSL certificate bundle",              True),
    ("trafilatura",  "trafilatura",   "Web page content extraction",         True),
    ("elevenlabs",   "elevenlabs",    "ElevenLabs TTS/STT (voice features)", False),
    ("webrtcvad-wheels", "webrtcvad", "Voice activity detection (live voice mode)", False),
    ("chromadb", "chromadb", "Vector database (semantic search)", True),
    ("onnxruntime", "onnxruntime", "ONNX runtime for bge-m3 embedding model", True),
    ("tokenizers", "tokenizers", "Fast tokenizer for bge-m3 ONNX model", True),
    ("croniter", "croniter", "Cron expression parser (scheduled prompts)", True),
    ("cron-descriptor", "cron_descriptor", "Human-readable cron descriptions (task schedules)", False),
]


# TEMPORARY: install spec for the pycord PR #3159 build (`fix/voice-rec-2`,
# DAVE voice-receive support). Used by _ensure_pycord_fix() when the fix is
# not yet present in the installed pycord. Remove once pycord ships a
# release containing the fix.
_PYCORD_PR_SPEC = (
    "py-cord[voice] @ "
    "git+https://github.com/Pycord-Development/pycord"
    "@326b72acc8d1d952ac002fe07ca65581cf5952bc"
)
# Commit (short) of the PR build above, used to recognize a git/PR install.
_PYCORD_PR_COMMIT_SHORT = "326b72a"


# Regex to peel an `==X.Y.Z` style version spec off a pip name like
# "py-cord[voice]==2.8.0". We ONLY honor `==` (exact pins); other
# operators (>=, <, ~=) are ignored by the version-mismatch check, which
# matches how pip itself treats range specs (no forced reinstall).
_PIN_RE = re.compile(r"^(?P<base>[^=<>!~ ]+(?:\[[^\]]+\])?)\s*==\s*(?P<ver>[^\s,]+)\s*$")


def parse_pin(pip_name):
    """Split a pip name into (base, version) when an exact `==` pin is present.

    Returns (base, version) on a hit, or (pip_name, None) when no pin is
    specified or the spec uses a non-`==` operator.

    The base preserves any extras suffix, e.g. "py-cord[voice]==2.8.0"
    splits to ("py-cord[voice]", "2.8.0"). The extras are kept on the
    base so the canonical-name lookup below still works (we strip them at
    that step).
    """
    m = _PIN_RE.match(pip_name)
    if not m:
        return pip_name, None
    return m.group("base"), m.group("ver")


def get_installed_version(pip_base):
    """Return the installed version string for a pip distribution, or None.

    `pip_base` may include extras (e.g. "py-cord[voice]"); we strip
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


def install_package(pip_name, force_reinstall=False, no_cache=False,
                    timeout=180):
    """Attempt to pip install a package. Returns (success, output).

    `pip_name` may include a version spec (e.g. "py-cord[voice]==2.8.0");
    pip honors it natively. `force_reinstall=True` adds `--force-reinstall`
    so an already-installed wrong-version package is replaced.
    `no_cache=True` adds `--no-cache-dir` (used for git URL installs so a
    stale wheel cache can't shadow the PR build).
    `timeout` is the maximum seconds to wait (default 180; callers may pass
    a higher value for heavy installs like git clones).
    """
    cmd = [sys.executable, "-m", "pip", "install"]
    if force_reinstall:
        cmd.append("--force-reinstall")
    if no_cache:
        cmd.append("--no-cache-dir")
    cmd.append(pip_name)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, f"Installation timed out ({timeout}s)"
    except Exception as e:
        return False, str(e)


# ── Windows native-build preflight + MSVC advisory ────────────────────────
# Packages in this set ship compiled C extensions but only publish prebuilt
# wheels for SOME Python versions. webrtcvad-wheels (daanzu fork) tops out
# at cp313 on Windows: on Python 3.14 pip finds no wheel and falls back to
# compiling the sdist, which needs the MSVC C++ toolset — specifically the
# "Desktop development with C++" workload of Visual Studio Build Tools, NOT
# just the Build Tools shell. Without it the build dies with:
#   error: Microsoft Visual C++ 14.0 or greater is required.
# The preflight below detects that situation up front so we can skip the
# doomed compile and print targeted guidance. Revisit the "Python 3.13"
# wording in the advisory if the fork ever ships cp314+ / abi3 wheels.
_NATIVE_OPTIONAL = {"webrtcvad-wheels"}

# Documentation site referenced by the advisory (mirrors README.md).
_DOC_URL = "https://ai-alcove.neocities.org/"

# Setuptools' missing-MSVC message, matched in pip's captured stderr as a
# post-failure backstop (covers any package and vswhere edge cases).
_MSVC_ERROR_SIGNATURE = "Microsoft Visual C++ 14.0 or greater is required"


def _print_pip_error_tail(output, indent="     ", max_lines=5, max_chars=120):
    """Show the tail of a failed pip command's captured output.

    The old print showed only the LAST stderr line truncated to 80 chars,
    which hid the real error behind pip's final "error: failed-wheel-build-
    for-install" wrapper (e.g. the missing-MSVC message inside the
    "N lines of output" block was invisible without re-running pip by hand).
    """
    lines = [ln for ln in (output or "").split("\n") if ln.strip()]
    if not lines:
        print(f"{indent}Unknown error")
        return
    for ln in lines[-max_lines:]:
        print(f"{indent}{ln.strip()[:max_chars]}")


def _pause_for_acknowledgment():
    """Block until the user presses Enter (C/R), so a critical advisory can't
    scroll past unread before the next installer section starts.

    Skipped automatically when stdin isn't an interactive terminal (piped /
    CI runs never block); EOF continues; Ctrl+C aborts the installer with
    the conventional SIGINT exit code so nothing is left hanging.
    """
    try:
        if not sys.stdin.isatty():
            return
        input("      Press Enter to continue... ")
    except EOFError:
        print()
    except KeyboardInterrupt:
        print("\n  Interrupted by user — aborting install.")
        sys.exit(130)


def _print_msvc_advisory(pip_name, failed=False):
    """Print targeted guidance for a native package that needs the MSVC C++
    toolset on Windows but cannot find it.

    `failed=True` means a source build was already attempted (the stderr
    trap fired); `failed=False` means the preflight saw it coming and we
    are skipping the install instead.
    """
    py_ver = sys.version.split()[0]
    print()
    print(f"  ⚠️  {pip_name}: no prebuilt wheel matches Python {py_ver} on")
    print( "      Windows, and the Microsoft Visual C++ toolset is not installed,")
    if failed:
        print( "      so pip's attempt to compile the package from source failed.")
    else:
        print( "      so pip would have to compile it from source. Skipping.")
    print()
    print( "      To fix, do ONE of:")
    print()
    print( "        1. Install the latest Microsoft C++ Build Tools:")
    print( "           https://visualstudio.microsoft.com/visual-cpp-build-tools/")
    print( "           In the installer, tick \"Desktop development with C++\"")
    print( "           (keep the MSVC v143 + Windows 11 SDK defaults), then re-run")
    print( "           install_deps.py. NOTE: installing the Build Tools alone is")
    print( "           NOT enough — the \"Desktop development with C++\" workload")
    print( "           checkbox is what actually provides the compiler.")
    print()
    print( "        2. Or run Alcove on Python 3.13, for which this package ships a")
    print( "           prebuilt wheel (no compiler needed at all).")
    if pip_name in _NATIVE_OPTIONAL:
        print()
        print( "      This package is optional — live voice (!joinLive) needs it,")
        print( "      but the rest of the bot runs fine without it.")
    print()
    print(f"      See the Alcove Upgrade Manual: {_DOC_URL}")
    print()
    # Force the user to acknowledge the advisory before the installer moves
    # on — without this, the very next section scrolls it away immediately.
    _pause_for_acknowledgment()


def _native_optional_preflight(pip_name):
    """Pre-install check for native optional packages on Windows.

    Returns "install" when the install should proceed, or "skip" when it
    cannot succeed (no prebuilt wheel AND no MSVC C++ toolset) — in which
    case the advisory has already been printed.

    Non-Windows platforms return "install" unconditionally: macOS/Linux
    ship a usable C compiler (clang/gcc), so a missing wheel just means a
    quick transparent compile, and the stderr trap covers the rare case
    where the compiler is absent there too.
    """
    if os.name != "nt":
        return "install"

    # Probe 1: does a prebuilt wheel match this Python/platform? Metadata-
    # only resolution (--dry-run downloads nothing). pip >= 22.2 supports
    # --dry-run; on older pips the flag itself errors, which we treat as
    # "inconclusive" and let the real install + trap handle it.
    try:
        probe = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run",
             "--only-binary=:all:", pip_name],
            capture_output=True, text=True, timeout=90,
        )
        if probe.returncode == 0:
            return "install"  # wheel exists — no compiler needed
        combined = (probe.stdout or "") + "\n" + (probe.stderr or "")
        if "no matching distribution" not in combined.lower():
            return "install"  # probe inconclusive — proceed; trap covers
    except Exception:
        return "install"

    # No wheel: a source build is required. Check for the MSVC C++ toolset
    # with vswhere — the same lookup setuptools' MSVC compiler wrapper
    # uses — so our answer matches what the build would actually see.
    # The component ID below is the one the "Desktop development with C++"
    # workload ticks; a Build Tools install WITHOUT that workload returns
    # nothing here, which is exactly the silent misconfiguration we want
    # to catch.
    vswhere = os.path.join(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "Microsoft Visual Studio", "Installer", "vswhere.exe",
    )
    try:
        result = subprocess.run(
            [vswhere, "-latest", "-products", "*",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return "install"  # toolset present — source build will succeed
    except Exception:
        pass  # vswhere itself missing => no VS product installed at all

    _print_msvc_advisory(pip_name, failed=False)
    return "skip"


def _explain_pip_failure(pip_name, output):
    """Post-failure backstop for the Windows preflight above.

    If a failed pip install's captured stderr carries the missing-MSVC
    signature, print the targeted advisory after the generic error output.
    Returns True when it fired.
    """
    if _MSVC_ERROR_SIGNATURE in (output or ""):
        _print_msvc_advisory(pip_name, failed=True)
        return True
    return False


# ── pycord PR #3159 tracking ──────────────────────────────────────────────
# TEMPORARY. These helpers ensure the installed pycord contains the DAVE
# voice-receive fix (PR #3159, branch `fix/voice-rec-2`). Remove once
# pycord ships a release containing the fix.
def _pycord_fix_present():
    """Return True if the installed pycord already has the DAVE voice-receive
    fix (PR #3159).

    Detected via the lowercase `has_davey` marker that PR #3159 introduced
    in discord.voice.utils.dependencies (renamed from `HAS_DAVEY`). This
    works for BOTH the PR/git build and any future merged pycord release
    that contains the fix — so we never backrev a newer fixed release.
    """
    try:
        import discord.voice.utils.dependencies as _dep
    except Exception:
        return False
    return hasattr(_dep, "has_davey")


def _pycord_is_pr_build():
    """Return True if the installed py-cord distribution looks like a git/PR
    build rather than a clean PyPI release. Used to avoid clobbering a
    hand-installed PR build during namespace-repair reinstalls.
    """
    try:
        ver = _ilm.version("py-cord")
    except Exception:
        return False
    return _PYCORD_PR_COMMIT_SHORT in ver.lower() or "+" in ver


def _ensure_pycord_fix(force_repair=False):
    """Ensure the installed pycord has the DAVE voice-receive fix (PR #3159).

    - fix present, force_repair=False -> no-op (idempotent).
    - fix present, force_repair=True  -> leave a git/PR build alone;
      reinstall stock "py-cord[voice]" only when the fixed build is a clean
      release (repairs the discord/ namespace after a conflict removal).
    - fix absent                      -> force-reinstall the PR build from
      the git URL with --no-cache-dir (also repairs the discord/ namespace).

    Returns True on success, False on failure.
    """
    fix_present = _pycord_fix_present()
    if fix_present and not force_repair:
        print("  ✅ pycord DAVE voice-receive fix already present (PR #3159)")
        return True
    if fix_present and force_repair:
        if _pycord_is_pr_build():
            print("  ✅ pycord PR build already installed (namespace intact)")
            return True
        print("  Re-installing py-cord[voice] (namespace repair)... ", end="", flush=True)
        success, output = install_package("py-cord[voice]", force_reinstall=True)
        if success:
            print("✅")
        else:
            print("❌")
            _print_pip_error_tail(output)
            _explain_pip_failure("py-cord[voice]", output)
        return success
    print("  Installing pycord PR #3159 (DAVE voice-receive fix)... ", end="", flush=True)
    success, output = install_package(
        _PYCORD_PR_SPEC,
        force_reinstall=True,
        no_cache=True,
        timeout=600,
    )
    if success:
        print("✅")
    else:
        print("❌")
        _print_pip_error_tail(output)
        _explain_pip_failure("py-cord PR #3159", output)
    return success


def preload_chromadb_embeddings():
    """Pre-download the bge-m3 ONNX embedding model.

    The model files live on HuggingFace under BAAI/bge-m3/onnx/ and are
    downloaded to ~/.cache/chroma/onnx_models/bge-m3/ (co-located with the
    old all-MiniLM-L6-v2 cache that ChromaDB's DefaultEmbeddingFunction
    used). Files are skip-if-present with size verification so the 2.27GB
    model.onnx_data can resume across flaky connections.

    The old ~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/ directory (166MB)
    is left untouched; it can be deleted manually after the first successful
    boot with bge-m3 to reclaim space.
    """
    import os as _os
    import ssl as _ssl
    import urllib.request as _urlreq

    _BGE_M3_BASE = "https://huggingface.co/BAAI/bge-m3/resolve/main/onnx"
    _CACHE_ROOT = _os.path.expanduser("~/.cache/chroma/onnx_models/bge-m3")

    # Build an HTTPS opener backed by certifi's CA bundle. macOS system Python
    # ships without root CA certs configured for urllib, which causes
    # CERTIFICATE_VERIFY_FAILED on every HTTPS request. certifi (already a
    # project dependency) provides the Mozilla CA bundle. If certifi isn't
    # importable (shouldn't happen — it's required and installed in Phase 1
    # before this runs), fall back to the default context, then to an
    # unverified context as a last resort (with a warning) so the download
    # at least progresses.
    _ssl_context = None
    try:
        import certifi as _certifi
        _ssl_context = _ssl.create_default_context(cafile=_certifi.where())
    except Exception:
        try:
            _ssl_context = _ssl.create_default_context()
        except Exception:
            _ssl_context = _ssl._create_unverified_context()
            print("  ⚠️  certifi not available — using unverified SSL context "
                  "(install certifi for proper certificate validation)")
    _https_handler = _urlreq.HTTPSHandler(context=_ssl_context)
    _opener = _urlreq.build_opener(_https_handler)

    # (filename, expected_size_bytes). Sizes verified from the HF repo at
    # the time of implementation. The size check is a sanity guard, not a
    # cryptographic verification — it catches truncated downloads.
    _FILES = [
        ("model.onnx", 725_000),                    # ~725 kB (graph)
        ("model.onnx_data", 2_270_000_000),         # ~2.27 GB (weights) — the big one
        ("sentencepiece.bpe.model", 5_070_000),     # ~5.07 MB (SP tokenizer)
        ("tokenizer.json", 17_100_000),             # ~17.1 MB (HF tokenizer)
        ("config.json", 698),                       # 698 B
        ("special_tokens_map.json", 964),           # 964 B
        ("tokenizer_config.json", 1_170),            # ~1.17 kB
        ("Constant_7_attr__value", 65_600),         # ~65.6 kB (ONNX constant)
    ]

    _os.makedirs(_CACHE_ROOT, exist_ok=True)

    print(f"  Target dir: {_CACHE_ROOT}")
    print(f"  Total download: ~2.29 GB (first install only; resumes on interruption)")
    print()

    all_present = True
    for fname, expected_approx in _FILES:
        dest = _os.path.join(_CACHE_ROOT, fname)
        # Skip if present and non-trivially sized (size check is approximate —
        # the expected sizes are rounded down from the exact HF sizes so a
        # complete download always passes; a truncated one is caught by the
        # 90% threshold below).
        if _os.path.exists(dest):
            actual = _os.path.getsize(dest)
            if actual >= int(expected_approx * 0.9):
                continue
            print(f"  ⚠️  {fname} present but truncated ({actual} < "
                  f"{int(expected_approx * 0.9)} bytes) — re-downloading")
            try:
                _os.remove(dest)
            except OSError:
                pass

        url = f"{_BGE_M3_BASE}/{fname}"
        size_mb = expected_approx / (1024 * 1024)
        size_str = f"{size_mb:.1f}MB" if size_mb >= 1 else f"{expected_approx}B"
        print(f"  Downloading {fname} ({size_str})... ", end="", flush=True)
        try:
            # Stream the download via the certifi-backed opener. We write
            # chunk-by-chunk to dest so the 2.27GB model.onnx_data never
            # loads fully into RAM (critical for small hosts). The opener's
            # HTTPS handler uses the certifi CA bundle for cert validation.
            _done = False
            with _opener.open(url) as _resp:
                with open(dest, "wb") as _f:
                    while True:
                        _chunk = _resp.read(1024 * 1024)  # 1MB chunks
                        if not _chunk:
                            break
                        _f.write(_chunk)
                _done = True
            if not _done:
                print("❌ download incomplete")
                all_present = False
                continue
            actual = _os.path.getsize(dest)
            if actual < int(expected_approx * 0.9):
                print(f"⚠️  truncated ({actual} bytes)")
                all_present = False
            else:
                print("✅")
        except Exception as e:
            print(f"❌ {e}")
            all_present = False

    print()
    if all_present:
        print("  ✅ bge-m3 ONNX model ready")
        return True, None
    else:
        print("  ⚠️  some files failed to download (vector search may not work)")
        return False, "incomplete download"


def main():
    print()
    print("=" * 50)
    print("  Alcove Dependency Installer")
    print("=" * 50)
    print()
    print(f"  Python: {sys.version.split()[0]} ({platform.machine()})")
    print(f"  Path:   {sys.executable}")
    print()

    already_installed = []
    to_install = []           # missing entirely
    to_correct_version = []   # installed but wrong pinned version
    results = []
    failures = []             # (pip_name, required) tuples for the summary
    skipped_optional = []     # native optionals skipped by the Windows preflight

    # Phase 0: Remove packages that conflict with our current dependencies.
    # py-cord and discord.py both claim the `discord` namespace — having
    # both installed causes import ambiguity. Likewise discord-ext-voice-recv
    # depends on discord.py and is no longer used. Remove them if present.
    # Each entry is (pip_name, reason, needs_pycord_namespace_restore).
    # The third element is True for packages that share the `discord/`
    # namespace with py-cord: removing them can leave stale namespace files
    # (e.g. discord/ext/__init__.py) that require a force-reinstall of
    # py-cord to repair. Packages that only own their own top-level module
    # (e.g. webrtcvad) set it False so their removal does NOT trigger a
    # py-cord force-reinstall — which would clobber a custom/PR py-cord build.
    _CONFLICTS = [
        ("discord.py", "Conflicts with py-cord (same `discord` namespace)", True),
        ("discord-ext-voice-recv", "Superseded by py-cord's native Sink API", True),
        ("webrtcvad", "Replaced by webrtcvad-wheels (maintained fork; the abandoned 2.0.10 breaks on setuptools 81+ via its unconditional `import pkg_resources`)", False),
    ]
    to_remove = []
    for pip_name, reason, _needs_ns_restore in _CONFLICTS:
        if get_installed_version(pip_name) is not None:
            to_remove.append((pip_name, reason, _needs_ns_restore))

    if to_remove:
        print("── Removing conflicting packages ──")
        print()
        for pip_name, reason, _needs_ns_restore in to_remove:
            print(f"  Removing {pip_name}... ", end="", flush=True)
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "uninstall", "-y", pip_name],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    print(f"✅  ({reason})")
                else:
                    print(f"⚠️  uninstall returned non-zero (may need manual removal)")
            except Exception as e:
                print(f"⚠️  {e}")
        # Only force-reinstall py-cord when a removed conflict actually shared
        # the `discord/` namespace with it (discord.py / discord-ext-voice-recv).
        # Removing webrtcvad does NOT touch the `discord/` namespace, so it
        # skips this step — avoiding an unnecessary reinstall that would
        # overwrite a custom/PR py-cord build with the stock PyPI release.
        # Routed through _ensure_pycord_fix() so a PR build (PR #3159) is left
        # intact rather than clobbered by a stock reinstall.
        if any(_needs_ns_restore for _, _, _needs_ns_restore in to_remove):
            if not _ensure_pycord_fix(force_repair=True):
                failures.append(("py-cord[voice] (PR #3159)", True))
            print()

    # Phase 0.5: Ensure pycord has the DAVE voice-receive fix (PR #3159).
    # If Phase 0 removed a namespace-sharing conflict, it already invoked
    # _ensure_pycord_fix(force_repair=True) above. Otherwise run it without
    # force_repair — it's a no-op when the fix is already present (PR build
    # or a future merged release) and only installs the PR build when the
    # fix is absent. TEMPORARY — remove once pycord ships the fix.
    if not (to_remove and any(n for _, _, n in to_remove)):
        print("── Ensuring pycord DAVE voice-receive fix (PR #3159) ──")
        print()
        if not _ensure_pycord_fix(force_repair=False):
            failures.append(("py-cord[voice] (PR #3159)", True))
        print()

    # Phase 1: Check what's installed (and at the correct version, when pinned)
    print("── Checking installed packages ──")
    print()
    for pip_name, import_name, description, required in DEPENDENCIES:
        tag = "required" if required else "optional"
        pip_base, pinned_ver = parse_pin(pip_name)

        # pycord is handled by _ensure_pycord_fix above (PR #3159 tracking),
        # so skip it here — the generic loop must not re-evaluate/install it.
        if pip_base == "py-cord[voice]":
            continue

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
        if not failures:
            print("  All dependencies are already installed at the correct versions!")
        # Still run post-install steps before returning.
        if check_installed("chromadb"):
            print()
            print("── Pre-downloading bge-m3 ONNX embedding model ──")
            print()
            preload_chromadb_embeddings()
        # If pycord PR failed in Phase 0/0.5, report it even on the early path.
        if failures:
            print()
            print(f"  ❌ REQUIRED packages that failed to install:")
            for name, _req in failures:
                print(f"       - {name}")
            print()
            return 1
        print()
        return 0

    # Phase 2a: Force-reinstall mismatched pinned versions FIRST.
    # Doing these before fresh installs means a downgrade/upgrade of e.g. py-cord
    # is in place before any package that depends on it gets touched.
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
                _print_pip_error_tail(output)
                _explain_pip_failure(pip_name, output)
                results.append((pip_name, False, output))
                failures.append((pip_name, required))

    # Phase 2b: Fresh-install missing packages.
    if to_install:
        print()
        print(f"── Installing {len(to_install)} missing package(s) ──")
        print()
        for pip_name, import_name, description, required in to_install:
            # Windows preflight for native optional packages (e.g. webrtcvad-
            # wheels): skip the doomed source build when no prebuilt wheel
            # matches and the MSVC C++ toolset is missing. The preflight has
            # already printed the fix-it advisory; the package is optional, so
            # this is NOT a failure — the bot runs without it.
            if (pip_name in _NATIVE_OPTIONAL
                    and _native_optional_preflight(pip_name) == "skip"):
                print(f"  ⚠️  {pip_name} skipped — see the advisory above")
                results.append((pip_name, False,
                                "Skipped (optional): no prebuilt wheel and "
                                "no MSVC C++ toolset on Windows"))
                skipped_optional.append(pip_name)
                continue
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
                _print_pip_error_tail(output, indent="           ")
                _explain_pip_failure(pip_name, output)
                results.append((pip_name, False, output))
                failures.append((pip_name, required))

    # Phase 2c: Pre-download ChromaDB embedding model.
    # Runs after chromadb has been installed/corrected above, or if it was
    # already present but other packages needed work.
    if check_installed("chromadb"):
        print()
        print("── Pre-downloading bge-m3 ONNX embedding model ──")
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
    if skipped_optional:
        print(f"  Skipped (optional): {len(skipped_optional)}  ({', '.join(skipped_optional)})")
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
        if skipped_optional:
            # Honest ending: something was skipped, so this was NOT a fully
            # successful install — live voice will be unavailable until the
            # user follows the advisory printed earlier in the run.
            print("  ⚠️  Installation completed WITH WARNINGS:")
            for name in skipped_optional:
                print(f"       - {name} was NOT installed — live voice (!joinLive)")
            print( "         will not work without it. See the advisory above for how")
            print( "         to fix this (C++ Build Tools workload, or Python 3.13),")
            print(f"         or the Alcove Upgrade Manual: {_DOC_URL}")
        else:
            print("  ✅ All dependencies are ready!")

    print()
    return 1 if any(req for _, req in failures) else 0


def _fix_command_scripts():
    """Ensure *.command scripts are executable and unquarantined on macOS/Linux."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(script_dir) == "utils":
        root_dir = os.path.dirname(script_dir)
    else:
        root_dir = script_dir

    import glob as _glob
    import stat as _stat

    if os.name != "nt":
        print()
        print("── Fixing .command script permissions ──")
        print()
        for cmd_file in _glob.glob(os.path.join(root_dir, "*.command")):
            os.chmod(cmd_file, _stat.S_IRWXU | _stat.S_IRGRP | _stat.S_IXGRP | _stat.S_IROTH | _stat.S_IXOTH)
            print(f"  chmod 755  {os.path.basename(cmd_file)}")

    if sys.platform == "darwin":
        if os.name == "nt":
            print()
            print("── Unquarantining .command scripts ──")
            print()
        for cmd_file in _glob.glob(os.path.join(root_dir, "*.command")):
            subprocess.run(["xattr", "-d", "com.apple.quarantine", cmd_file], check=False)
            print(f"  unquarantine  {os.path.basename(cmd_file)}")


if __name__ == "__main__":
    rc = main()
    _fix_command_scripts()
    sys.exit(rc)
