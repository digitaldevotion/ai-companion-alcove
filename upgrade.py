#!/usr/bin/env python3
# ============================================
# Alcove — upgrade.py
# Interactive upgrade assistant: migrates personal files from a previous
# Alcove installation and merges config settings with the new template.
# ============================================
#
# What this script does:
#   1. Prompts for the path to the OLD Alcove directory.
#   2. Validates that it looks like a real Alcove install.
#   3. Copies personal files into this (new) directory:
#        - config.py
#        - companion_data.db
#        - companion_datafiles/  (entire folder)
#   4. Backs up the freshly copied config.py.
#   5. Merges new config_template.py settings into config.py,
#      preserving all existing user values.
#   6. Prints a summary and next steps.
#
# Usage:
#   cd /path/to/new-alcove
#   python3 upgrade.py          (Mac / Linux)
#   python  upgrade.py          (Windows)

import ast
import os
import sys

# ── Auto-activate ~/alcove-env (macOS / Linux only) ─────────────────
if os.name != "nt" and not os.environ.get("VIRTUAL_ENV") and sys.prefix == sys.base_prefix:
    _venv_dir = os.path.expanduser("~/alcove-env")
    _venv_python = os.path.join(_venv_dir, "bin", "python3")
    if os.path.isdir(_venv_dir) and os.path.isfile(_venv_python):
        os.environ["VIRTUAL_ENV"] = _venv_dir
        os.environ["PATH"] = os.path.join(_venv_dir, "bin") + os.pathsep + os.environ.get("PATH", "")
        os.execv(_venv_python, [_venv_python] + sys.argv)

import re
import shutil
from datetime import datetime
from pathlib import Path

from modules.upgrade_config import collect_assignments, merge_config, parse_overrides

HERE = Path(__file__).parent.resolve()
TEMPLATE_PATH = HERE / "config_template.py"
CONFIG_PATH = HERE / "config.py"
OVERRIDES_PATH = HERE / "modules" / "upgrade_overrides.dat"
DB_NAME = "companion_data.db"
DATAFILES_DIR = "companion_datafiles"
TOOLS_DIR = "tools"

# Python executable name for displaying example commands back to the user.
# Uses the name of whatever interpreter is currently running this script
# (e.g. "python3" on macOS/Linux, "python.exe" on Windows), stripped of
# the .exe suffix so copy-pasted commands look natural.
PY_CMD = Path(sys.executable).stem or "python"


# ── Tool-location reconciliation ───────────────────────────────────────────

def _tool_filenames(tools_dir):
    """Return a set of .md filenames in a tools directory (just the names, not paths)."""
    if not tools_dir.is_dir():
        return set()
    return {f.name for f in tools_dir.iterdir() if f.is_file() and f.suffix == ".md"}


def _extract_tool_filename(entry_text):
    """Given a source fragment like 'Path(__file__).parent / "tools/react.md"',
    return just the filename portion ('react.md'), or None if unparseable."""
    m = re.search(r'["\']tools/([^"\']+)["\']', entry_text)
    return m.group(1) if m else None


def reconcile_tool_locations(config_source, old_tools_dir, new_tools_dir):
    """Adjust LOADED_TOOL_LOCATIONS in *config_source* based on which tool
    files were removed or added between the old and new installs.

    Returns (new_source, removed_list, added_list).
    """
    old_files = _tool_filenames(old_tools_dir)
    new_files = _tool_filenames(new_tools_dir)

    gone = old_files - new_files      # files that were removed / renamed away
    fresh = new_files - old_files     # files that are brand-new in this release

    if not gone and not fresh:
        return config_source, [], []

    # Locate the LOADED_TOOL_LOCATIONS list in the source via AST so we have
    # exact line numbers, then do a text-level rewrite.
    tree = ast.parse(config_source)
    target_node = None
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "LOADED_TOOL_LOCATIONS"):
            target_node = node
            break

    if target_node is None:
        return config_source, [], []

    lines = config_source.splitlines(keepends=True)

    # The list spans from target_node.value.lineno to target_node.value.end_lineno (1-based).
    list_start = target_node.value.lineno - 1      # index into lines[]
    list_end = target_node.value.end_lineno - 1

    # Pull out the existing list lines and filter / augment.
    list_lines = lines[list_start:list_end + 1]

    # --- Remove entries whose tool file no longer exists ---
    removed = []
    kept_lines = []
    for line in list_lines:
        fname = _extract_tool_filename(line)
        if fname and fname in gone:
            removed.append(fname)
        else:
            kept_lines.append(line)

    # --- Add entries for brand-new tool files ---
    # Find the line just before the closing ']' so we can insert above it.
    # Detect indentation from an existing entry for consistency.
    indent = "        "  # fallback
    for line in kept_lines:
        if "tools/" in line:
            indent = line[: len(line) - len(line.lstrip())]
            break

    added = []
    # We insert new tools just before the closing bracket.
    insert_before = len(kept_lines) - 1  # the ']' line

    if fresh and insert_before > 0:
        # The last real entry before ']' may lack a trailing comma (valid
        # Python for the final element).  We need to add one before we
        # insert new entries after it, or the result won't parse.
        last_entry_idx = insert_before - 1
        stripped = kept_lines[last_entry_idx].rstrip()
        # Strip inline comment to find the actual code portion
        code_part = stripped.split("#")[0].rstrip() if "#" in stripped else stripped
        if code_part and not code_part.endswith(","):
            # Insert a comma right after the code, preserving any comment
            if "#" in stripped:
                comment_start = stripped.index("#")
                kept_lines[last_entry_idx] = (
                    stripped[:comment_start].rstrip() + ","
                    + "  " + stripped[comment_start:] + "\n"
                )
            else:
                kept_lines[last_entry_idx] = stripped + ",\n"

    for fname in sorted(fresh):
        entry = f'{indent}Path(__file__).parent / "tools/{fname}",\n'
        kept_lines.insert(insert_before, entry)
        insert_before += 1
        added.append(fname)

    # Reassemble the source.
    new_source = "".join(lines[:list_start] + kept_lines + lines[list_end + 1:])

    # Sanity check — must still parse.
    ast.parse(new_source)

    return new_source, removed, added


# ── Main upgrade flow ──────────────────────────────────────────────────────

def ask(prompt, default=None):
    """Prompt the user for input."""
    if default:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "
    answer = input(prompt).strip()
    return answer if answer else default


def main():
    print()
    print("=" * 56)
    print("  Alcove Upgrade Assistant")
    print("=" * 56)
    print()
    print("This script migrates your personal files and settings")
    print("from an existing Alcove installation into this new one.")
    print()

    # ── 0. Make sure the old bot is stopped ──
    answer = ask("Did you shut down the existing Alcove process? (Y/N)")
    if not answer or answer.lower() != "y":
        print()
        print("   Please stop the bot first (Ctrl+C in its terminal or whatever process),")
        print("   you normally follow then re-run this script.")
        return 1
    print()

    # ── 1. Validate this directory looks like a new Alcove install ──
    if not TEMPLATE_PATH.exists():
        print(f"❌ config_template.py not found in this directory.")
        print(f"   Run this script from inside the NEW Alcove folder.")
        return 1

    # ── 2. Ask for the old directory ──
    old_path_str = ask("Enter the path to your PREVIOUS Alcove directory\n")
    if not old_path_str:
        print("❌ No path provided. Exiting.")
        return 1

    old_dir = Path(old_path_str).resolve()

    # Validate it's not the same directory
    if old_dir == HERE:
        print()
        print("❌ That's the same directory as this one!")
        print("   The old Alcove directory must be a DIFFERENT folder.")
        print(f"   This (new) directory: {HERE}")
        return 1

    # Validate it looks like an Alcove install
    if not old_dir.is_dir():
        print(f"❌ Directory not found: {old_dir}")
        return 1

    old_config = old_dir / "config.py"
    if not old_config.exists():
        print(f"❌ No config.py found in {old_dir}")
        print("   Are you sure that's an Alcove directory?")
        return 1

    # ── 2b. Check if the new directory is next to the old one ──
    old_parent = old_dir.parent
    if HERE.parent != old_parent:
        print()
        print(f"⚠  This new directory isn't next to your old install.")
        print(f"   Old install:  {old_dir}")
        print(f"   Running from: {HERE}")
        print()
        expected_dest = old_parent / HERE.name
        if expected_dest.exists():
            print(f"❌ Cannot auto-relocate: {expected_dest} already exists.")
            print(f"   Remove or rename it first, or move this folder manually.")
            return 1
        answer = ask(f"Move this directory to {expected_dest}? (Y/N)")
        if not answer or answer.lower() != "y":
            print()
            print("   Continuing from the current location. You can move it later.")
        else:
            print(f"\n   Moving {HERE.name}/ → {old_parent}/ ... ", end="")
            try:
                shutil.copytree(HERE, expected_dest)
                print("✅")
                print()
                print(f"   ✅ Copied to: {expected_dest}")
                print()
                print(f"   Please re-run the upgrade from the new location:")
                print(f"     cd \"{expected_dest}\"")
                print(f"     {PY_CMD} upgrade.py")
                print()
                print(f"   (You can delete {HERE} after confirming the new location works.)")
                return 0
            except Exception as e:
                print(f"❌")
                print(f"   Failed to copy: {e}")
                print(f"   Move the folder manually and re-run.")
                return 1

    print()
    print(f"   Old directory: {old_dir}")
    print(f"   New directory: {HERE}")
    print()

    # ── 2c. Ensure companion_datafiles/ auto-discovery subdirectories exist ──
    # Create these in the NEW install BEFORE the copy. The companion_datafiles
    # copy below uses dirs_exist_ok=True so old files are merged in on top of
    # these pre-created subdirs rather than wiping them. main.py's periodic
    # auto-discovery then populates the matching config variables from
    # whatever the user drops into each folder.
    print("── Ensuring auto-discovery subdirectories ──")
    AUTO_DISCOVER_SUBDIRS = [
        "1_system_prompt",
        "2_secondary_instructions",
        "3_context_history",
        "4_context_reference",
        "5_search_reference",
    ]
    new_datafiles_pre = HERE / DATAFILES_DIR
    new_datafiles_pre.mkdir(exist_ok=True)
    for sub in AUTO_DISCOVER_SUBDIRS:
        sub_path = new_datafiles_pre / sub
        if sub_path.is_dir():
            print(f"   {DATAFILES_DIR}/{sub}/ ... ✅ exists")
        else:
            sub_path.mkdir(parents=True, exist_ok=True)
            print(f"   {DATAFILES_DIR}/{sub}/ ... ➕ created")
    print()

    # ── 3. Copy personal files ──
    print("── Copying personal files ──")
    files_copied = 0

    # config.py
    print(f"   config.py ... ", end="")
    shutil.copy2(old_config, CONFIG_PATH)
    print("✅")
    files_copied += 1

    # companion_data.db
    old_db = old_dir / DB_NAME
    if old_db.exists():
        print(f"   {DB_NAME} ... ", end="")
        shutil.copy2(old_db, HERE / DB_NAME)
        print(f"✅ ({old_db.stat().st_size / 1024:.0f} KB)")
        files_copied += 1
    else:
        print(f"   {DB_NAME} ... ⬜ not found (starting fresh)")

    # companion_datafiles/
    old_datafiles = old_dir / DATAFILES_DIR
    new_datafiles = HERE / DATAFILES_DIR
    if old_datafiles.is_dir():
        print(f"   {DATAFILES_DIR}/ ... ", end="")
        # Count files for reporting
        file_count = sum(1 for _ in old_datafiles.rglob("*") if _.is_file())
        # Merge old files into the (already-created) new datafiles tree so
        # the auto-discovery subdirs created above survive. dirs_exist_ok=True
        # requires Python 3.8+.
        shutil.copytree(old_datafiles, new_datafiles, dirs_exist_ok=True)
        print(f"✅ ({file_count} files)")
        files_copied += 1
    else:
        print(f"   {DATAFILES_DIR}/ ... ⬜ not found (using defaults)")

    print(f"\n   {files_copied} item(s) copied.")

    # ── 4. Back up the (now-copied) config.py ──
    print()
    print("── Merging config settings ──")

    stamp = datetime.now().strftime("%Y%m%d")
    backup_path = HERE / f"config_backup_{stamp}.py"
    if backup_path.exists():
        stamp_full = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = HERE / f"config_backup_{stamp_full}.py"
    shutil.copy2(CONFIG_PATH, backup_path)
    print(f"   📦 Config backup: {backup_path.name}")

    # ── 5. Merge template + user config ──
    template_src = TEMPLATE_PATH.read_text()
    user_src = CONFIG_PATH.read_text()

    try:
        template_assigns = collect_assignments(template_src)
    except SyntaxError as e:
        print(f"   ❌ Syntax error in {TEMPLATE_PATH.name}: {e}")
        return 1

    try:
        user_assigns = collect_assignments(user_src)
    except SyntaxError as e:
        print(f"   ❌ Syntax error in your config.py: {e}")
        print(f"      Fix the error and re-run. Your backup is at {backup_path.name}.")
        return 1

    try:
        overrides = parse_overrides(OVERRIDES_PATH)
        new_source, report = merge_config(template_src, user_src, overrides=overrides)
    except SyntaxError as e:
        print(f"   ❌ Merged config has a syntax error: {e}")
        print(f"      config.py is unchanged. Backup: {backup_path.name}")
        return 1

    # ── 5b. Reconcile LOADED_TOOL_LOCATIONS ──
    old_tools = old_dir / TOOLS_DIR
    new_tools = HERE / TOOLS_DIR
    tools_removed = []
    tools_added = []
    print()
    print(f"   Old tools dir: {old_tools}  (exists: {old_tools.is_dir()})")
    print(f"   New tools dir: {new_tools}  (exists: {new_tools.is_dir()})")
    if old_tools.is_dir() or new_tools.is_dir():
        try:
            old_set = _tool_filenames(old_tools)
            new_set = _tool_filenames(new_tools)
            print(f"   Old tool files: {sorted(old_set)}")
            print(f"   New tool files: {sorted(new_set)}")
            print(f"   Gone (in old, not new): {sorted(old_set - new_set)}")
            print(f"   Fresh (in new, not old): {sorted(new_set - old_set)}")
            new_source, tools_removed, tools_added = reconcile_tool_locations(
                new_source, old_tools, new_tools
            )
        except Exception as e:
            print(f"   ⚠  Tool location reconciliation failed: {e}")
            print(f"      LOADED_TOOL_LOCATIONS left as-is; review it manually.")

    CONFIG_PATH.write_text(new_source)

    print()
    print(f"   ✅ Merged {report['total']} template variables into config.py")
    print(f"      • {len(report['preserved'])} values preserved from your old config")
    for n in report["preserved"]:
        print(f"          ~ {n}")
    print(f"      • {len(report['unchanged'])} values already matched the template")
    print(f"      • {len(report['added'])} new variables added with template defaults")
    for n in report["added"]:
        print(f"          + {n}")
    if report["overridden"]:
        print(f"      • {len(report['overridden'])} variable(s) force-replaced with template value (see upgrade_overrides.dat)")
        for n in report["overridden"]:
            print(f"          ! {n}")

    if report["removed"]:
        print()
        print(f"   ⚠  {len(report['removed'])} variable(s) from your old config are NOT in the new template:")
        for n in report["removed"]:
            print(f"          ? {n}")
        print("      These were dropped from config.py but remain in your backup.")

    if tools_removed or tools_added:
        print()
        print("   🔧 LOADED_TOOL_LOCATIONS updated:")
        for t in tools_removed:
            print(f"          − {t}  (removed — no longer in tools/)")
        for t in tools_added:
            print(f"          + {t}  (new tool)")
        if tools_removed:
            print("      Removed tools were renamed or retired. Check the release")
            print("      notes if you relied on one — the replacement may already")
            print("      be in the added list above.")

    # ── 6. Install / verify Python dependencies ──
    print()
    print("── Checking Python dependencies ──")
    print()
    deps_rc = 0
    try:
        import install_deps
        deps_rc = install_deps.main()
    except Exception as e:
        print(f"   ⚠  Could not run dependency installer: {e}")
        print(f"      Run it manually with:  {PY_CMD} install_deps.py")
        deps_rc = 1

    # ── 7. Next steps ──
    print()
    print("=" * 56)
    print("  Upgrade complete!")
    print("=" * 56)
    print()
    print("  Next steps:")
    print()
    print("  1. Review config.py — especially any NEW settings listed above.")
    print()
    if deps_rc != 0:
        print("  ⚠  Some Python dependencies could not be installed (see above).")
        print(f"     Re-run the installer once the issue is resolved:")
        print(f"       {PY_CMD} install_deps.py")
        print()
    print("  2. Start Alcove from THIS directory and note any errors or failures:")
    print(f"       {PY_CMD} alcove.py")
    print()
    print("  3. Inside of your companion's Discord, run !diag in any channel to verify everything is working.")
    print()
    print("  4. If everything looks good, this is now your Alcove directory.")
    print(f"     Keep your old directory ({old_dir.name}/) around for 30 days")
    print("     as a safety net, then delete it.")
    print()
    print(f"  Config backup: {backup_path.name}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
