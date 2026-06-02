#!/usr/bin/env python3
"""
Cleanup script for Alcove — removes companion data, database, config,
and other generated files to restore the directory to a clean state.
"""

import os
import shutil
import sys

# Files and directories to remove, relative to the script's directory.
# Each entry is (path, is_directory, clear_contents_only).
TARGETS = [
    ("companion_data.db",      False, False),
    ("config.py",              False, False),
    ("companion_datafiles",    True,  True),   # remove contents, keep directory
    ("search_vectors_data",    True,  True),   # remove contents, keep directory
    (".claude",                True,  False),
    (".DS_Store",              False, False),
    ("__pycache__",            True,  False)
]

CONFIRMATION_PHRASE = "ERASE_ALL"

# Inside companion_datafiles/, these subdirectories are preserved (their
# contents are erased, but the folders themselves are always kept so
# main.py's auto-discovery scan keeps working after cleanup).
PRESERVED_DATAFILE_SUBDIRS = {
    "1_system_prompt",
    "2_secondary_instructions",
    "3_context_history",
    "4_context_reference",
    "5_search_reference",
}


def _empty_directory(path):
    # Recursively delete everything inside *path*, but leave *path* itself.
    for entry in os.listdir(path):
        entry_path = os.path.join(path, entry)
        if os.path.isdir(entry_path):
            shutil.rmtree(entry_path)
        else:
            os.remove(entry_path)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    print()
    print("=" * 60)
    print("  WARNING: THIS OPERATION IS HIGHLY DESTRUCTIVE")
    print("=" * 60)
    print()
    print("This will permanently erase the following from")
    print(f"  {script_dir}")
    print()
    print("  - Companion database  (companion_data.db)")
    print("  - Configuration       (config.py, pyvenv.cfg)")
    print("  - Companion datafiles (contents of companion_datafiles/)")
    print("  - Virtual-env dirs    (bin/, include/, lib/)")
    print("  - Documentation, tests, caches, and other generated files")
    print()
    print("This action CANNOT be undone.")
    print()

    try:
        response = input(f'Type {CONFIRMATION_PHRASE} and press ENTER to proceed: ')
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)

    if response.strip() != CONFIRMATION_PHRASE:
        print("Confirmation not received — aborting. No files were removed.")
        sys.exit(1)

    print()

    for name, is_dir, contents_only in TARGETS:
        path = os.path.join(script_dir, name)

        if contents_only:
            # Remove everything inside the directory but keep the directory itself.
            # Inside companion_datafiles/, the auto-discovery subdirs listed in
            # PRESERVED_DATAFILE_SUBDIRS are also kept (emptied, not removed)
            # so main.py's auto-discovery scan still finds them post-cleanup.
            if os.path.isdir(path):
                entries = os.listdir(path)
                if not entries:
                    print(f"  Skipping  {name}/  (already empty)")
                    continue
                is_datafiles = (name == "companion_datafiles")
                for entry in entries:
                    entry_path = os.path.join(path, entry)
                    if (is_datafiles
                            and entry in PRESERVED_DATAFILE_SUBDIRS
                            and os.path.isdir(entry_path)):
                        _empty_directory(entry_path)
                        print(f"  Cleared   {name}/{entry}/*  (kept folder)")
                    elif os.path.isdir(entry_path):
                        shutil.rmtree(entry_path)
                    else:
                        os.remove(entry_path)
                print(f"  Cleared   {name}/*")
            else:
                print(f"  Skipping  {name}/  (not found)")
        elif is_dir:
            if os.path.isdir(path):
                shutil.rmtree(path)
                print(f"  Removed   {name}/")
            else:
                print(f"  Skipping  {name}/  (not found)")
        else:
            if os.path.isfile(path):
                os.remove(path)
                print(f"  Removed   {name}")
            else:
                print(f"  Skipping  {name}  (not found)")

    print()
    print("Cleanup complete.")


if __name__ == "__main__":
    main()
