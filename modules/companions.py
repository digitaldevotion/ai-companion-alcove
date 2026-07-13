# ============================================
# Alcove — companions.py
# Companion and datafile management
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import os
import shutil
from pathlib import Path

import config
from .database import get_channel_companion

_AUTO_DISCOVER_BASE = Path(__file__).parent.parent
_COMPANION_DATAFILES_DIR = _AUTO_DISCOVER_BASE / "companion_datafiles"
_TOOLS_DIR = _AUTO_DISCOVER_BASE / "tools"


def load_file_content(path):
    # Return the file's text, or None if the path is empty/missing/unreadable.
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            print(f"\u26a0\ufe0f File not found (skipping): {path}")
        except UnicodeDecodeError as e:
            print(f"\u26a0\ufe0f Binary/non-text file (skipping): {path} \u2014 {e}")
        except OSError as e:
            print(f"\u26a0\ufe0f Could not read file (skipping): {path} \u2014 {e}")
    return None


def _migrate_legacy_datafiles():
    # Upgrade standard legacy layout:
    # If companion_datafiles/ contains folders like "1_system_prompt" directly,
    # but does NOT have a "default" directory, create "default" and move
    # legacy companion content into it.
    legacy_subs = ["1_system_prompt", "2_secondary_instructions", "3_context_history", "4_context_reference", "5_search_reference"]
    default_dir = _COMPANION_DATAFILES_DIR / "default"

    has_legacy = any((_COMPANION_DATAFILES_DIR / sub).is_dir() for sub in legacy_subs)
    if has_legacy and not default_dir.is_dir():
        print("\U0001f69a Legacy single-companion files found in companion_datafiles/ \u2014 migrating to companion_datafiles/default/... ")
        default_dir.mkdir(parents=True, exist_ok=True)
        for sub in legacy_subs:
            old_sub = _COMPANION_DATAFILES_DIR / sub
            if old_sub.is_dir():
                new_sub = default_dir / sub
                if os.path.exists(new_sub):
                    shutil.copytree(old_sub, new_sub, dirs_exist_ok=True)
                    shutil.rmtree(old_sub)
                else:
                    os.rename(old_sub, new_sub)
        print("\u2705 Legacy files migrated successfully.")


# Run legacy migration at boot
_migrate_legacy_datafiles()


def get_companion_paths(companion_name="default"):
    """
    Get resolving directories for a specific companion.
    If the requested companion name directory does not exist, fall back to "default".
    """
    comp_dir = _COMPANION_DATAFILES_DIR / companion_name
    if not comp_dir.is_dir():
        comp_dir = _COMPANION_DATAFILES_DIR / "default"

    comp_dir.mkdir(parents=True, exist_ok=True)

    return {
        "SYSTEM_PROMPT_DIR": comp_dir / "1_system_prompt",
        "INSTRUCTION_DIR": comp_dir / "2_secondary_instructions",
        "CONTEXT_HISTORY_DIR": comp_dir / "3_context_history",
        "CONTEXT_REFERENCE_DIR": comp_dir / "4_context_reference",
        "SEARCH_REFERENCE_DIR": comp_dir / "5_search_reference",
    }


def _scan_dir_for_files(dir_path):
    # Recursive scan; skip hidden files (e.g. .DS_Store).
    if not dir_path.is_dir():
        return []
    return sorted(
        p for p in dir_path.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )


def _scan_tool_files(dir_path):
    # Non-recursive scan of the shared tools/ directory. Only .md files are
    # treated as tool definitions (matches the convention used by the
    # upgrade assistant). Hidden files (e.g. .DS_Store) are skipped.
    if not dir_path.is_dir():
        return []
    return sorted(
        p for p in dir_path.iterdir()
        if p.is_file() and p.suffix == ".md" and not p.name.startswith(".")
    )


def get_companion_resolved_locations(db, channel_name):
    """
    Returns resolved lists of file paths for the active companion of this channel.
    Respects user overrides in config.py if they were customized (only when on default companion).
    """
    active_companion = get_channel_companion(channel_name)
    paths = get_companion_paths(active_companion)

    # 1. System Prompt
    system_prompt_loc = ""
    if active_companion == "default":
        system_prompt_loc = getattr(config, "SYSTEM_PROMPT_LOCATION", "")

    if not system_prompt_loc:
        sys_files = _scan_dir_for_files(paths["SYSTEM_PROMPT_DIR"])
        if sys_files:
            system_prompt_loc = sys_files[0]

    # 2. Lists
    resolved = {
        "SYSTEM_PROMPT_LOCATION": system_prompt_loc,
        "INSTRUCTION_LOCATIONS": [],
        "CONTEXT_HISTORY_LOCATIONS": [],
        "CONTEXT_REFERENCE_LOCATIONS": [],
        "SEARCH_REFERENCE_LOCATIONS": [],
        "LOADED_TOOL_LOCATIONS": [],
    }

    mapping = {
        "INSTRUCTION_LOCATIONS": "INSTRUCTION_DIR",
        "CONTEXT_HISTORY_LOCATIONS": "CONTEXT_HISTORY_DIR",
        "CONTEXT_REFERENCE_LOCATIONS": "CONTEXT_REFERENCE_DIR",
        "SEARCH_REFERENCE_LOCATIONS": "SEARCH_REFERENCE_DIR",
    }

    for attr, key in mapping.items():
        user_configured = getattr(config, attr, None) if active_companion == "default" else None
        if user_configured:
            resolved[attr] = user_configured
        else:
            resolved[attr] = _scan_dir_for_files(paths[key])

    # 3. Tool definitions
    # Tools live in the project-root tools/ directory and are shared across
    # all companions. A user can override the auto-discovered set by
    # populating config.LOADED_TOOL_LOCATIONS (only honored for the default
    # companion, matching the override semantics above).
    user_tools = getattr(config, "LOADED_TOOL_LOCATIONS", None) if active_companion == "default" else None
    if user_tools:
        resolved["LOADED_TOOL_LOCATIONS"] = list(user_tools)
    else:
        resolved["LOADED_TOOL_LOCATIONS"] = _scan_tool_files(_TOOLS_DIR)

    return resolved
