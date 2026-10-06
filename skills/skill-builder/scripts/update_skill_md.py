#!/usr/bin/env python3
"""
Skill.md Updater - Backs up the current skill.md and replaces it with a temporary copy

Usage:
    update_skill_md.py <path/to/skill-folder> <path/to/temp-skill.md> [backup-directory]

Examples:
    update_skill_md.py skills/public/my-skill /tmp/skill.md.new
    update_skill_md.py skills/public/my-skill /tmp/skill.md.new ./backups
"""

import sys
import shutil
from datetime import datetime
from pathlib import Path


def update_skill_md(skill_path, temp_skill_md, backup_dir=None):
    """
    Back up the current skill.md and replace it with a temporary copy.

    Args:
        skill_path: Path to the skill folder containing skill.md
        temp_skill_md: Path to the temporary copy of skill.md to write
        backup_dir: Optional backup directory (defaults to the skill directory itself)

    Returns:
        Path to the backup file, or None if error
    """
    skill_path = Path(skill_path).resolve()
    temp_skill_md = Path(temp_skill_md).resolve()

    # Validate skill folder exists
    if not skill_path.exists():
        print(f"❌ Error: Skill folder not found: {skill_path}")
        return None

    if not skill_path.is_dir():
        print(f"❌ Error: Path is not a directory: {skill_path}")
        return None

    # Validate skill.md exists in skill folder
    skill_md = skill_path / "skill.md"
    if not skill_md.exists():
        print(f"❌ Error: skill.md not found in {skill_path}")
        return None

    # Validate temp file exists
    if not temp_skill_md.exists():
        print(f"❌ Error: Temporary skill.md not found: {temp_skill_md}")
        return None

    if not temp_skill_md.is_file():
        print(f"❌ Error: Temporary path is not a file: {temp_skill_md}")
        return None

    # Determine backup location (default: skill directory itself)
    if backup_dir:
        backup_path = Path(backup_dir).resolve()
        backup_path.mkdir(parents=True, exist_ok=True)
    else:
        backup_path = skill_path

    # Create timestamped backup of current skill.md
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_filename = f"skill.md.bak-{timestamp}"
    backup_file = backup_path / backup_filename

    try:
        shutil.copy2(skill_md, backup_file)
        print(f"✅ Backed up current skill.md to: {backup_file}")
    except Exception as e:
        print(f"❌ Error creating backup: {e}")
        return None

    # Overwrite skill.md with the temporary copy
    try:
        shutil.copy2(temp_skill_md, skill_md)
        print(f"✅ Replaced skill.md with: {temp_skill_md}")
    except Exception as e:
        print(f"❌ Error replacing skill.md: {e}")
        print(f"   Backup is preserved at: {backup_file}")
        return None

    print(f"\n✅ Successfully updated skill.md")
    print(f"   Backup: {backup_file}")
    return backup_file


def main():
    if len(sys.argv) < 3:
        print("Usage: python update_skill_md.py <path/to/skill-folder> <path/to/temp-skill.md> [backup-directory]")
        print("\nExamples:")
        print("  python update_skill_md.py skills/public/my-skill /tmp/skill.md.new")
        print("  python update_skill_md.py skills/public/my-skill /tmp/skill.md.new ./backups")
        sys.exit(1)

    skill_path = sys.argv[1]
    temp_skill_md = sys.argv[2]
    backup_dir = sys.argv[3] if len(sys.argv) > 3 else None

    print(f"📝 Updating skill.md in: {skill_path}")
    print(f"   Source: {temp_skill_md}")
    if backup_dir:
        print(f"   Backup directory: {backup_dir}")
    print()

    result = update_skill_md(skill_path, temp_skill_md, backup_dir)

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
