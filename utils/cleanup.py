#!/usr/bin/env python3
"""
Cleanup script for Alcove — removes companion data, database, config,
and other generated files to restore the directory to a clean state.
"""

import glob
import os
import shutil
import stat
import subprocess
import sys

# Files and directories to remove, relative to the script's directory.
# Each entry is (path, is_directory, clear_contents_only).
TARGETS = [
    ("config.py",              False, False),
    ("companion_datafiles",    True,  True),
    ("databases",              True,  True),
    (".claude",                True,  False),
    ("CLAUDE.md",              False, False),
    (".DS_Store",              False, False),
    ("*.pid",                  False, False)
]

CONFIRMATION_PHRASE = "ERASE_ALL"

# Inside companion_datafiles/, these subdirectories are preserved (their
# contents are erased, but the folders themselves are always kept so
# main.py's auto-discovery scan keeps working after cleanup).
PRESERVED_DATAFILE_SUBDIRS = {
    "default",
    "1_system_prompt",
    "2_secondary_instructions",
    "3_context_history",
    "4_context_reference",
    "5_search_reference",
}

NUMERIC_PREFIXED_SUBDIRS = {
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
    if os.path.basename(script_dir) == "utils":
        root_dir = os.path.dirname(script_dir)
    else:
        root_dir = script_dir

    print()
    print("=" * 60)
    print("  WARNING: THIS OPERATION IS HIGHLY DESTRUCTIVE")
    print("=" * 60)
    print()
    print("This will permanently erase the following from")
    print(f"  {root_dir}")
    print()
    print("  - Companion databases (contents of databases/)")
    print("  - Configuration       (config.py)")
    print("  - Companion datafiles (contents of companion_datafiles/)")
    print("  - __pycache__ directories (everywhere in the tree)")
    print("  - Documentation, caches, and other generated files")
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
        path = os.path.join(root_dir, name)

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
                if is_datafiles:
                    for entry in entries:
                        entry_path = os.path.join(path, entry)
                        if entry in PRESERVED_DATAFILE_SUBDIRS and os.path.isdir(entry_path):
                            if entry == "default":
                                for sub in os.listdir(entry_path):
                                    sub_path = os.path.join(entry_path, sub)
                                    if sub in NUMERIC_PREFIXED_SUBDIRS and os.path.isdir(sub_path):
                                        _empty_directory(sub_path)
                                        print(f"  Cleared   {name}/{entry}/{sub}/*  (kept folder)")
                                    elif os.path.isdir(sub_path):
                                        shutil.rmtree(sub_path)
                                        print(f"  Removed   {name}/{entry}/{sub}/")
                                    else:
                                        os.remove(sub_path)
                                        print(f"  Removed   {name}/{entry}/{sub}")
                            else:
                                _empty_directory(entry_path)
                                print(f"  Cleared   {name}/{entry}/*  (kept folder)")
                        elif os.path.isdir(entry_path):
                            shutil.rmtree(entry_path)
                            print(f"  Removed   {name}/{entry}/")
                        else:
                            os.remove(entry_path)
                            print(f"  Removed   {name}/{entry}")
                else:
                    for entry in entries:
                        entry_path = os.path.join(path, entry)
                        if os.path.isdir(entry_path):
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
            if "*" in name:
                matches = glob.glob(path)
                if matches:
                    for match in matches:
                        os.remove(match)
                        print(f"  Removed   {os.path.basename(match)}")
                else:
                    print(f"  Skipping  {name}  (not found)")
            elif os.path.isfile(path):
                os.remove(path)
                print(f"  Removed   {name}")
            else:
                print(f"  Skipping  {name}  (not found)")

    for dirpath, dirnames, _ in os.walk(root_dir, topdown=True):
        for dirname in dirnames:
            if dirname == "__pycache__":
                pycache_path = os.path.join(dirpath, dirname)
                shutil.rmtree(pycache_path)
                rel = os.path.relpath(pycache_path, root_dir)
                print(f"  Removed   {rel}/")
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]

    print()
    print("Cleanup complete.")

    if os.name != "nt":
        for cmd_file in glob.glob(os.path.join(root_dir, "*.command")):
            os.chmod(cmd_file, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            print(f"  chmod 755  {os.path.basename(cmd_file)}")

    if sys.platform == "darwin":
        for cmd_file in glob.glob(os.path.join(root_dir, "*.command")):
            subprocess.run(["xattr", "-d", "com.apple.quarantine", cmd_file], check=False)
            print(f"  unquarantine  {os.path.basename(cmd_file)}")


if __name__ == "__main__":
    main()
