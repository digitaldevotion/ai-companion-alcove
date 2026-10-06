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
#        - databases/companion_data.db
#        - companion_datafiles/  (entire folder)
#        - skills/  (personal skill folders not already present)
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

import shutil
from datetime import datetime
from pathlib import Path

# Add project root and utils dir to sys.path so modules and other utils can be imported
HERE = Path(__file__).parent.resolve()
if HERE.name == "utils":
    ROOT = HERE.parent
else:
    ROOT = HERE

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from modules.upgrade_config import collect_assignments, merge_config, parse_overrides

TEMPLATE_PATH = HERE / "config_template.py"
CONFIG_PATH = ROOT / "config.py"
OVERRIDES_PATH = HERE / "upgrade_overrides.dat"
DATAFILES_DIR = "companion_datafiles"

# Python executable name for displaying example commands back to the user.
# Uses the name of whatever interpreter is currently running this script
# (e.g. "python3" on macOS/Linux, "python.exe" on Windows), stripped of
# the .exe suffix so copy-pasted commands look natural.
PY_CMD = Path(sys.executable).stem or "python"


def _is_location_var_empty(source, var_name):
    """Return True if *var_name* is not defined in *source* or is set to an
    empty string / empty list (i.e. the user relies on auto-discovery)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == var_name):
            val = node.value
            if isinstance(val, ast.Constant) and val.value == "":
                return True
            if isinstance(val, ast.List) and len(val.elts) == 0:
                return True
            return False
    return True


# ── Main upgrade flow ──────────────────────────────────────────────────────

def ask(prompt, default=None):
    """Prompt the user for input."""
    if default:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "
    answer = input(prompt).strip()
    return answer if answer else default


def _run_upgrade():
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

    # ── 1b. List previous Alcove directories in parent folder ──
    previous_alcove_dirs = []
    parent_dir = ROOT.parent
    if parent_dir.is_dir():
        for entry in sorted(parent_dir.iterdir()):
            if entry.is_dir() and entry != ROOT and (entry / "config.py").exists():
                previous_alcove_dirs.append(entry)

    if previous_alcove_dirs:
        print()
        print("   Previous Alcove directories found in parent folder:")
        for d in previous_alcove_dirs:
            print(f"      • {d}")
        print()

    # ── 2. Ask for the old directory ──
    old_path_str = ask("Enter the path to your PREVIOUS Alcove directory you wish to upgrade from\n")
    if not old_path_str:
        print("❌ No path provided. Exiting.")
        return 1

    old_dir = Path(old_path_str).resolve()

    # Validate it's not the same directory
    if old_dir == ROOT:
        print()
        print("❌ That's the same directory as this one!")
        print("   The old Alcove directory must be a DIFFERENT folder.")
        print(f"   This (new) directory: {ROOT}")
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
    if ROOT.parent != old_parent:
        print()
        print(f"⚠  This new directory isn't next to your old install.")
        print(f"   Old install:  {old_dir}")
        print(f"   Running from: {ROOT}")
        print()
        expected_dest = old_parent / ROOT.name
        if expected_dest.exists():
            print(f"❌ Cannot auto-relocate: {expected_dest} already exists.")
            print(f"   Remove or rename it first, or move this folder manually.")
            return 1
        answer = ask(f"Move this directory to {expected_dest}? (Y/N)")
        if not answer or answer.lower() != "y":
            print()
            print("   Continuing from the current location. You can move it later.")
        else:
            print(f"\n   Moving {ROOT.name}/ → {old_parent}/ ... ", end="")
            try:
                shutil.copytree(ROOT, expected_dest)
                print("✅")
                print()
                print(f"   ✅ Copied to: {expected_dest}")
                print()
                print(f"   Please re-run the upgrade from the new location:")
                print(f"     cd \"{expected_dest}\"")
                print(f"     {PY_CMD} upgrade.py")
                print()
                print(f"   (You can delete {ROOT} after confirming the new location works.)")
                return 0
            except Exception as e:
                print(f"❌")
                print(f"   Failed to copy: {e}")
                print(f"   Move the folder manually and re-run.")
                return 1

    print()
    print(f"   Old directory: {old_dir}")
    print(f"   New directory: {ROOT}")
    print()

    # ── 2c. Ensure companion_datafiles/ auto-discovery subdirectories exist ──
    # Create the default/ profile directory and its auto-discovery subdirs in
    # the NEW install BEFORE the copy. The companion_datafiles copy below uses
    # dirs_exist_ok=True so old files are merged in on top of these pre-created
    # subdirs rather than wiping them. main.py's periodic auto-discovery then
    # populates the matching config variables from whatever the user drops into
    # each folder.
    print("── Ensuring auto-discovery subdirectories ──")
    AUTO_DISCOVER_SUBDIRS = [
        "1_system_prompt",
        "2_secondary_instructions",
        "3_context_history",
        "4_context_reference",
        "5_search_reference",
    ]
    new_datafiles_pre = ROOT / DATAFILES_DIR
    new_datafiles_pre.mkdir(exist_ok=True)
    default_dir = new_datafiles_pre / "default"
    if default_dir.is_dir():
        print(f"   {DATAFILES_DIR}/default/ ... ✅ exists")
    else:
        default_dir.mkdir(parents=True, exist_ok=True)
        print(f"   {DATAFILES_DIR}/default/ ... ➕ created")
    for sub in AUTO_DISCOVER_SUBDIRS:
        sub_path = default_dir / sub
        if sub_path.is_dir():
            print(f"   {DATAFILES_DIR}/default/{sub}/ ... ✅ exists")
        else:
            sub_path.mkdir(parents=True, exist_ok=True)
            print(f"   {DATAFILES_DIR}/default/{sub}/ ... ➕ created")
    print()

    # ── 3. Copy personal files ──
    print("── Copying personal files ──")
    files_copied = 0

    # config.py
    print(f"   config.py ... ", end="")
    shutil.copy2(old_config, CONFIG_PATH)
    print("✅")
    files_copied += 1

    # databases/ — copy the entire tree (all companion DBs + registry.db).
    # Excludes search_vectors_data/ (vector stores rebuild on startup) and
    # hidden files like .DS_Store.
    old_dbs_dir = old_dir / "databases"
    if old_dbs_dir.is_dir():
        (ROOT / "databases").mkdir(exist_ok=True)
        print(f"   databases/ ... ", end="")

        def _ignore_dbs_tree(directory, contents):
            return [c for c in contents
                    if c == "search_vectors_data" or c.startswith(".")]

        shutil.copytree(old_dbs_dir, ROOT / "databases",
                        dirs_exist_ok=True, ignore=_ignore_dbs_tree)
        db_count = sum(1 for p in (ROOT / "databases").rglob("*.db") if p.is_file())
        if db_count:
            print(f"✅ ({db_count} database file(s))")
            files_copied += 1
        else:
            print("⬜ (no .db files found)")
    else:
        # Pre-2.0 layout: companion_data.db lived at the install root
        old_root_db = old_dir / "companion_data.db"
        if old_root_db.exists():
            (ROOT / "databases").mkdir(exist_ok=True)
            dest = ROOT / "databases" / "companion_data.db"
            print(f"   databases/companion_data.db ... ", end="")
            shutil.copy2(old_root_db, dest)
            print(f"✅ ({old_root_db.stat().st_size / 1024:.0f} KB)")
            files_copied += 1
        else:
            print(f"   databases/ ... ⬜ not found (starting fresh)")

    # companion_datafiles/
    old_datafiles = old_dir / DATAFILES_DIR
    new_datafiles = ROOT / DATAFILES_DIR
    if old_datafiles.is_dir():
        print(f"   {DATAFILES_DIR}/ ... ", end="")
        file_count = sum(1 for _ in old_datafiles.rglob("*") if _.is_file())

        # Determine which top-level auto-discovery folders to divert into
        # default/ instead of copying to the root of companion_datafiles/.
        LOCATION_VAR_TO_FOLDER = {
            "SYSTEM_PROMPT_LOCATION": "1_system_prompt",
            "INSTRUCTION_LOCATIONS": "2_secondary_instructions",
            "CONTEXT_HISTORY_LOCATIONS": "3_context_history",
            "CONTEXT_REFERENCE_LOCATIONS": "4_context_reference",
            "SEARCH_REFERENCE_LOCATIONS": "5_search_reference",
        }
        old_config_src = old_config.read_text()
        divert_folders = set()
        for var_name, folder_name in LOCATION_VAR_TO_FOLDER.items():
            if _is_location_var_empty(old_config_src, var_name):
                divert_folders.add(folder_name)

        def _ignore_top_auto_discovery(directory, contents):
            if directory == str(old_datafiles):
                return [c for c in contents if c in divert_folders]
            return []

        shutil.copytree(old_datafiles, new_datafiles, dirs_exist_ok=True,
                        ignore=_ignore_top_auto_discovery)
        print(f"✅ ({file_count} files)")
        files_copied += 1

        # Copy diverted auto-discovery folders into default/ instead.
        migrated = []
        for folder_name in sorted(divert_folders):
            src_folder = old_datafiles / folder_name
            dst_folder = new_datafiles / "default" / folder_name
            if not src_folder.is_dir():
                continue
            src_file_count = sum(1 for _ in src_folder.rglob("*") if _.is_file())
            if src_file_count == 0:
                continue
            shutil.copytree(src_folder, dst_folder, dirs_exist_ok=True)
            migrated.extend(
                f"default/{folder_name}/{f.relative_to(src_folder)}"
                for f in src_folder.rglob("*")
                if f.is_file()
            )
        if migrated:
            print(f"   → {len(migrated)} auto-discovered file(s) diverted into {DATAFILES_DIR}/default/:")
            for f in migrated:
                print(f"      → {f}")
    else:
        print(f"   {DATAFILES_DIR}/ ... ⬜ not found (using defaults)")

    # skills/ — copy personal skill directories from the old install. A skill
    # folder is only copied when it does NOT already exist in the new install
    # (so built-in / updated skills win) and its name does NOT start with
    # "test-" (test skills are disposable and skipped on upgrade).
    old_skills = old_dir / "skills"
    new_skills = ROOT / "skills"
    if old_skills.is_dir():
        new_skills.mkdir(exist_ok=True)

        def _ignore_hidden(directory, contents):
            return [c for c in contents if c.startswith(".")]

        copied_skills = []
        skipped_skills = 0
        for entry in sorted(old_skills.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith("test-"):
                skipped_skills += 1
                continue
            dest = new_skills / name
            if dest.exists():
                skipped_skills += 1
                continue
            shutil.copytree(entry, dest, ignore=_ignore_hidden)
            copied_skills.append(name)

        if copied_skills or skipped_skills:
            print(f"   skills/ ... ", end="")
            if copied_skills:
                detail = f"{len(copied_skills)} copied"
                if skipped_skills:
                    detail += f", {skipped_skills} skipped"
                print(f"✅ ({detail})")
                for n in copied_skills:
                    print(f"      → {n}")
                files_copied += 1
            else:
                print(f"⬜ ({skipped_skills} skipped, none to copy)")
        else:
            print(f"   skills/ ... ⬜ (no skill folders in old install)")
    else:
        print(f"   skills/ ... ⬜ not found (using defaults)")

    # *.pid files — copy any PID files (e.g. alcove.pid) from the old install
    # so start.command can kill the previous process by PID instead of falling
    # back to a broad pkill that may hit unrelated Alcove processes.
    old_pid_files = sorted(old_dir.glob("*.pid"))
    if old_pid_files:
        print(f"   *.pid ... ", end="")
        for pid_file in old_pid_files:
            shutil.copy2(pid_file, ROOT / pid_file.name)
        print(f"✅ ({len(old_pid_files)} pid file(s))")
        files_copied += 1
    else:
        print(f"   *.pid ... ⬜ not found (start.command will use pkill fallback)")

    print(f"\n   {files_copied} item(s) copied.")

    # ── 4. Back up the (now-copied) config.py ──
    print()
    print("── Merging config settings ──")

    stamp = datetime.now().strftime("%Y%m%d")
    backup_path = ROOT / f"config_backup_{stamp}.py"
    if backup_path.exists():
        stamp_full = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = ROOT / f"config_backup_{stamp_full}.py"
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
    print(f"       {PY_CMD} modules/alcove.py")
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


def main():
    try:
        return _run_upgrade()
    except (KeyboardInterrupt, EOFError):
        print()
        print("Cancelled by user.")
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
