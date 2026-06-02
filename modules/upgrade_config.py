#!/usr/bin/env python3
# ============================================
# upgrade_config.py — merge a new config_template.py into an existing config.py
# ============================================
#
# Rules:
#   * Structure (order, comments, section headers, blank lines, imports)
#     comes from config_template.py.
#   * Top-level variable VALUES come from the existing config.py whenever
#     that variable is also present there. New variables introduced by the
#     template use the template's default value.
#   * A timestamped backup of config.py is written BEFORE any changes:
#         config_backup_<YYYYMMDD>.py
#     (if one already exists for today, a _HHMMSS suffix is appended so
#     nothing is overwritten).
#
# Limitations:
#   * Only top-level `NAME = value` and `NAME: annotation = value`
#     assignments are merged. Tuple unpacking, augmented assignment, and
#     assignments nested inside `if` / `try` blocks are left as the
#     template has them.
#   * Inline comments a user may have added next to a modified value are
#     NOT carried over — the template's comments win. If a comment needs
#     to survive upgrades, put it in the template.
#   * Variables that exist in the user's config but NOT in the template
#     are dropped from the new config.py. They are still present in the
#     backup, and the script prints their names so the user can decide
#     whether to re-add them manually.

import ast
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent.parent
TEMPLATE_PATH = HERE / "config_template.py"
CONFIG_PATH = HERE / "config.py"
OVERRIDES_PATH = Path(__file__).parent / "upgrade_overrides.dat"


def parse_overrides(path=None):
    # Read the override file and return a set of variable names. Lines
    # starting with # and blank lines are ignored. Returns an empty set
    # if the file doesn't exist.
    if path is None:
        path = OVERRIDES_PATH
    path = Path(path)
    if not path.is_file():
        return set()
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        names.add(stripped)
    return names


def collect_assignments(source):
    # Walk top-level statements and return {name: value_node} for every
    # simple assignment. Only single-target Name assignments are included.
    tree = ast.parse(source)
    out = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            out[node.targets[0].id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            out[node.target.id] = node.value
    return out


def splice(source, replacements):
    # Replace byte-ranges in `source`. Each replacement is a 5-tuple:
    #   (start_line, start_col, end_line, end_col, new_text)
    # where lines are 1-indexed and columns are byte offsets within the
    # line (matching Python's ast module). Applied right-to-left so
    # earlier offsets don't shift under us.
    src_bytes = source.encode("utf-8")
    line_starts = [0]
    for i, b in enumerate(src_bytes):
        if b == 0x0A:  # '\n'
            line_starts.append(i + 1)

    def pos(line, col):
        return line_starts[line - 1] + col

    spans = [
        (pos(sl, sc), pos(el, ec), new.encode("utf-8"))
        for sl, sc, el, ec, new in replacements
    ]
    spans.sort(key=lambda t: t[0], reverse=True)

    out = src_bytes
    for start, end, new in spans:
        out = out[:start] + new + out[end:]
    return out.decode("utf-8")


def merge_config(template_src, user_src, overrides=None):
    # Merge user values into the template. Returns (new_source, report)
    # where report is a dict with keys: total, preserved, unchanged, added,
    # overridden, removed. Raises SyntaxError if either input — or the merged
    # result — fails to parse.
    #
    # If `overrides` is a set of variable names, those variables always use
    # the template value — the user's existing value is discarded. Overridden
    # variables are reported separately so the upgrade summary can call them out.
    if overrides is None:
        overrides = set()
    template_assigns = collect_assignments(template_src)
    user_assigns = collect_assignments(user_src)

    replacements = []
    preserved = []      # user value was kept (and differed from template default)
    unchanged = []      # variable present in both, values already match
    added = []          # template introduced this; user didn't have it
    overridden = []     # user value existed but was force-replaced with template value
    for name, template_value_node in template_assigns.items():
        if name not in user_assigns:
            added.append(name)
            continue

        user_value_node = user_assigns[name]
        user_text = ast.get_source_segment(user_src, user_value_node)
        template_text = ast.get_source_segment(template_src, template_value_node)
        if user_text is None or template_text is None:
            # Shouldn't happen on 3.8+, but fail safe: don't touch this one.
            unchanged.append(name)
            continue

        if user_text.strip() == template_text.strip():
            unchanged.append(name)
            continue

        if name in overrides:
            # Template value wins — do NOT add a splice replacement.
            overridden.append(name)
            continue

        replacements.append((
            template_value_node.lineno,
            template_value_node.col_offset,
            template_value_node.end_lineno,
            template_value_node.end_col_offset,
            user_text,
        ))
        preserved.append(name)

    removed = [n for n in user_assigns if n not in template_assigns]
    new_source = splice(template_src, replacements)

    # Sanity check: the result should still parse.
    ast.parse(new_source)

    return new_source, {
        "total": len(template_assigns),
        "preserved": preserved,
        "unchanged": unchanged,
        "added": added,
        "overridden": overridden,
        "removed": removed,
    }


def main():
    if not TEMPLATE_PATH.exists():
        print(f"❌ Template not found: {TEMPLATE_PATH.name}")
        return 1
    if not CONFIG_PATH.exists():
        print(f"❌ Existing config not found: {CONFIG_PATH.name}")
        print("   First-time setup? Copy config_template.py to config.py manually,")
        print("   edit your tokens and paths, then re-run this script on future upgrades.")
        return 1

    template_src = TEMPLATE_PATH.read_text()
    user_src = CONFIG_PATH.read_text()

    # --- Back up the current config.py BEFORE touching anything ---
    stamp = datetime.now().strftime("%Y%m%d")
    backup_path = HERE / f"config_backup_{stamp}.py"
    if backup_path.exists():
        stamp_full = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = HERE / f"config_backup_{stamp_full}.py"
    shutil.copy2(CONFIG_PATH, backup_path)
    print(f"📦 Backup written: {backup_path.name}")

    try:
        overrides = parse_overrides()
        new_source, report = merge_config(template_src, user_src, overrides=overrides)
    except SyntaxError as e:
        print(f"❌ Syntax error while merging config: {e}")
        print(f"   config.py is UNCHANGED. Backup is still at {backup_path.name}.")
        return 1

    CONFIG_PATH.write_text(new_source)

    # --- Report ---
    print()
    print(f"✅ Merged {report['total']} template variables into config.py")
    print(f"   • {len(report['preserved'])} values preserved from your existing config")
    for n in report["preserved"]:
        print(f"       ~ {n}")
    print(f"   • {len(report['unchanged'])} values already matched the template")
    print(f"   • {len(report['added'])} new variables pulled in from template defaults")
    for n in report["added"]:
        print(f"       + {n}")
    if report["overridden"]:
        print(f"   • {len(report['overridden'])} variable(s) force-replaced with template value (see upgrade_overrides.dat)")
        for n in report["overridden"]:
            print(f"       ! {n}")

    if report["removed"]:
        print()
        print(f"⚠  {len(report['removed'])} variable(s) in your old config are NOT in the template:")
        for n in report["removed"]:
            print(f"       ? {n}")
        print("   These were dropped from the new config.py. They remain in the backup.")
        print("   Review them and re-add manually if they're still needed.")

    print()
    print(f"   Backup: {backup_path.name}")
    print( "   Review the new config.py before running the bot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
