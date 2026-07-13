#!/usr/bin/env python3
# ============================================
# Alcove — Create New Companion Tool
# Helper script to bootstrap a new companion folder structure automatically.
# ============================================

import os
import re
import sys
from pathlib import Path

# ── Auto-activate ~/alcove-env (macOS / Linux only) ─────────────────
if os.name != "nt" and not os.environ.get("VIRTUAL_ENV") and sys.prefix == sys.base_prefix:
    _venv_dir = os.path.expanduser("~/alcove-env")
    _venv_python = os.path.join(_venv_dir, "bin", "python3")
    if os.path.isdir(_venv_dir) and os.path.isfile(_venv_python):
        os.environ["VIRTUAL_ENV"] = _venv_dir
        os.environ["PATH"] = os.path.join(_venv_dir, "bin") + os.pathsep + os.environ.get("PATH", "")
        os.execv(_venv_python, [_venv_python] + sys.argv)


def ask(prompt, default=None):
    if default:
        parsed_prompt = f"{prompt} [{default}]: "
    else:
        parsed_prompt = f"{prompt}: "
    answer = input(parsed_prompt).strip()
    return answer if answer else default


def _run_create():
    print()
    print("=" * 56)
    print("  Alcove — New Companion Bootstrapper")
    print("=" * 56)
    print()
    print("This utility will automatically set up the folder structure")
    print("for a new AI Companion. You can then drop their customized")
    print("personality, prompt, and reference files into these folders.")
    print()
    print("NOTE: You only need to use this utility if you are creating")
    print("an additional companion beyond the first (default) one.")
    print()

    # Determine paths
    here = Path(__file__).parent.resolve()
    if here.name == "utils":
        root = here.parent
    else:
        root = here
    companion_datafiles_dir = root / "companion_datafiles"

    if not companion_datafiles_dir.is_dir():
        print(f"❌ Error: 'companion_datafiles' directory not found at: {companion_datafiles_dir}")
        print("   Make sure to run this script from the root of your Alcove application.")
        return 1

    # Ask for companion name
    name = ask("Enter the name of your new companion (e.g., Sherlock, Alice)")
    if not name:
        print("❌ Error: No companion name provided. Exiting.")
        return 1

    # Normalize name (no spaces, pure safe alphanumeric path strings)
    normalized_name = re.sub(r'[^a-zA-Z0-9_\-]+', '', name).strip().lower()

    if not normalized_name:
        print("❌ Error: Invalid companion name. Must contain alphanumeric characters.")
        return 1

    # Enforce safe directory name comparison
    if normalized_name.lower() == "default":
        print("❌ Error: 'default' is reserved for the primary fallback companion.")
        print("   Please use a unique custom name for your new companion.")
        return 1

    target_dir = companion_datafiles_dir / normalized_name

    if target_dir.is_dir():
        print()
        print(f"❌ Error: Companion directory already exists.")
        print(f"   A companion named '{normalized_name}' is already configured at:")
        print(f"   {target_dir}")
        return 1

    print()
    print(f"Creating workspace for companion: **{normalized_name}**")
    print("-" * 56)

    # Subfolders to create
    subdirs = [
        "1_system_prompt",
        "2_secondary_instructions",
        "3_context_history",
        "4_context_reference",
        "5_search_reference",
    ]

    for sub in subdirs:
        sub_path = target_dir / sub
        sub_path.mkdir(parents=True, exist_ok=True)
        print(f"  ➕ Created: {normalized_name}/{sub}/")

    # Generate a starter base system prompt template
    starter_prompt_file = target_dir / "1_system_prompt" / "prompt.txt"
    starter_prompt_content = (
        f"You are {normalized_name}, a friendly AI companion.\n"
        f"Embark on adventures, chat deeply, and behave in line with your name!\n"
    )

    starter_prompt_file.write_text(starter_prompt_content, encoding="utf-8")
    print(f"  📝 Created starter prompt template at: {normalized_name}/1_system_prompt/prompt.txt")

    print()
    print("=" * 56)
    print("🎉 Success! Companion files is ready!")
    print("=" * 56)
    print()
    print(f"Your companion folders have been created at:")
    print(f"📂 {target_dir}")
    print()
    print("Next steps:")
    print(f"  1. Paste or write your custom data files inside those directories.")
    print("  2. Start or restart Alcove.")
    print(f"  3. In Discord, switch this channel's companion on-demand by calling:")
    print(f"     !switchCompanion {normalized_name}")
    print()
    return 0


def main():
    try:
        return _run_create()
    except (KeyboardInterrupt, EOFError):
        print()
        print("Cancelled by user.")
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
