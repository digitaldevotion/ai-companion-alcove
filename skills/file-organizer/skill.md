---
skill name: file-organizer
description: Intelligently organizes your files and folders across your computer by understanding context, finding duplicates, suggesting better structures, and automating cleanup tasks. Reduces cognitive load and keeps your digital workspace tidy without manual effort.
---

# File Organizer

This skill acts as your personal organization assistant, helping you maintain a clean, logical file structure across your computer without the mental overhead of constant manual organization.

## What This Skill Does

1. **Analyzes Current Structure**: Reviews your folders and files to understand what you have
2. **Finds Duplicates**: Identifies duplicate files across your system
3. **Suggests Organization**: Proposes logical folder structures based on your content
4. **Automates Cleanup**: Moves, renames, and organizes files with your approval
5. **Maintains Context**: Makes smart decisions based on file types, dates, and content
6. **Reduces Clutter**: Identifies old files you probably don't need anymore

## Examples of the types of requests the user might make

```
Help me organize my Downloads folder
```

```
Find duplicate files in my Documents folder
```

```
Review my project directories in C:\myProjects and suggest improvements
```

```
Organize the files in my Downloads folder into proper subfolders based on what they are
```

```
Find duplicate files and help me decide which to keep
```

```
Clean up old files I haven't touched in 6+ months
```

```
Create a better folder structure for my [work/projects/photos/etc]
```

## Instructions

When a user requests file organization help:

1. **Determine the Operating System**

   Detect which OS the skill is running on so the correct command syntax is used throughout. Run the appropriate detection command:

   ```bash
   # Linux / macOS (any POSIX shell)
   uname -o 2>/dev/null || uname -s
   ```

   ```powershell
   # Windows (PowerShell)
   $env:OS
   # Or check via .NET:
   [System.Environment]::OSVersion.Platform
   ```

   Interpret the output:
   - `GNU/Linux`, `Darwin`, `Linux`, or `Darwin.*` → use the **Linux/macOS (bash)** commands
   - `Windows` (from `$env:OS`) or `Win32NT` (from `.Platform`) → use the **Windows (PowerShell)** commands

   Store the detected OS in your context for the remainder of the session. On Windows, prefer PowerShell commands unless the user explicitly requests Command Prompt (cmd) syntax. When a step below provides two command blocks, choose the one matching the detected OS.

2. **Understand the Scope**
   
   Ask clarifying questions:
   - Which directory needs organization? (Downloads, Documents, entire home folder?)
   - What's the main problem? (Can't find things, duplicates, too messy, no structure?)
   - Any files or folders to avoid? (Current projects, sensitive data?)
   - How aggressively to organize? (Conservative vs. comprehensive cleanup)

3. **Analyze Current State**
   
   Review the target directory using the RUNCMD tool call to gather more information. Use the block matching your detected OS:

   **Linux / macOS (bash)**
   ```bash
   # Get overview of current structure
   ls -la [target_directory]

   # Check file types and sizes
   find [target_directory] -type f -exec file {} \; | head -20

   # Identify largest files
   du -sh [target_directory]/* | sort -rh | head -20

   # Count file types
   find [target_directory] -type f | sed 's/.*\.//' | sort | uniq -c | sort -rn
   ```

   **Windows (PowerShell)**
   ```powershell
   # Get overview of current structure
   Get-ChildItem -Force [target_directory]

   # Check file types and sizes (first 20)
   Get-ChildItem -Path [target_directory] -File -Recurse | Select-Object -First 20 Name, Length, @{n='Type';e={$_.Extension}}

   # Identify largest files (top 20)
   Get-ChildItem -Path [target_directory] -Recurse -File |
     Sort-Object Length -Descending | Select-Object -First 20 Name, @{n='MB';e={[math]::Round($_.Length/1MB,2)}}

   # Count file types
   Get-ChildItem -Path [target_directory] -Recurse -File |
     Group-Object Extension | Sort-Object Count -Descending |
     Select-Object Count, Name
   ```
   
   Summarize findings:
   - Total files and folders
   - File type breakdown
   - Size distribution
   - Date ranges
   - Obvious organization issues

4. **Identify Organization Patterns**
   
   Based on the files, determine logical groupings:
   
   **By Type**:
   - Documents (PDFs, DOCX, TXT)
   - Images (JPG, PNG, SVG)
   - Videos (MP4, MOV)
   - Archives (ZIP, TAR, DMG)
   - Code/Projects (directories with code)
   - Spreadsheets (XLSX, CSV)
   - Presentations (PPTX, KEY)
   
   **By Purpose**:
   - Work vs. Personal
   - Active vs. Archive
   - Project-specific
   - Reference materials
   - Temporary/scratch files
   
   **By Date**:
   - Current year/month
   - Previous years
   - Very old (archive candidates)

5. **Find Duplicates**
   
   When requested, search for duplicates using the block matching your detected OS:

   **Linux / macOS (bash)**
   ```bash
   # Find exact duplicates by hash
   find [directory] -type f -exec md5 {} \; | sort | uniq -d

   # Find files with same name
   find [directory] -type f -printf '%f\n' | sort | uniq -d

   # Find similar-sized files
   find [directory] -type f -printf '%s %p\n' | sort -n
   ```

   **Windows (PowerShell)**
   ```powershell
   # Find exact duplicates by hash
   Get-ChildItem -Path [directory] -File -Recurse |
     Get-FileHash -Algorithm MD5 |
     Group-Object Hash | Where-Object { $_.Count -gt 1 }

   # Find files with same name
   Get-ChildItem -Path [directory] -File -Recurse |
     Group-Object Name | Where-Object { $_.Count -gt 1 } |
     Select-Object Name, Count

   # Find similar-sized files (group by size)
   Get-ChildItem -Path [directory] -File -Recurse |
     Sort-Object Length | Select-Object Length, FullName
   ```
   
   For each set of duplicates:
   - Show all file paths
   - Display sizes and modification dates
   - Recommend which to keep (usually newest or best-named)
   - **Important**: Always ask for confirmation before deleting

6. **Propose Organization Plan**
   
   Present a clear plan before making changes:
   
   ```markdown
   # Organization Plan for [Directory]
   
   ## Current State
   - X files across Y folders
   - [Size] total
   - File types: [breakdown]
   - Issues: [list problems]
   
   ## Proposed Structure
   
   ```
   [Directory]/
   ├── Work/
   │   ├── Projects/
   │   ├── Documents/
   │   └── Archive/
   ├── Personal/
   │   ├── Photos/
   │   ├── Documents/
   │   └── Media/
   └── Downloads/
       ├── To-Sort/
       └── Archive/
   ```
   
   ## Changes I'll Make
   
   1. **Create new folders**: [list]
   2. **Move files**:
      - X PDFs → Work/Documents/
      - Y images → Personal/Photos/
      - Z old files → Archive/
   3. **Rename files**: [any renaming patterns]
   4. **Delete**: [duplicates or trash files]
   
   ## Files Needing Your Decision
   
   - [List any files you're unsure about]
   
   Ready to proceed? (yes/no/modify)
   ```

7. **Execute Organization**
   
   After approval, organize systematically using the block matching your detected OS:

   **Linux / macOS (bash)**
   ```bash
   # Create folder structure
   mkdir -p "path/to/new/folders"

   # Move files with clear logging
   mv "old/path/file.pdf" "new/path/file.pdf"

   # Rename files with consistent patterns
   # Example: "YYYY-MM-DD - Description.ext"
   ```

   **Windows (PowerShell)**
   ```powershell
   # Create folder structure (creates all intermediate folders)
   New-Item -ItemType Directory -Force -Path "path\to\new\folders"

   # Move files with clear logging
   Move-Item -Path "old\path\file.pdf" -Destination "new\path\file.pdf"

   # Rename files with consistent patterns
   # Example: "YYYY-MM-DD - Description.ext"
   Rename-Item -Path "old-name.pdf" -NewName "2024-10-17-description.pdf"
   ```
   
   **Important Rules**:
   - Always confirm before deleting anything
   - Log all moves for potential undo
   - Preserve original modification dates
   - Handle filename conflicts gracefully
   - Stop and ask if you encounter unexpected situations

8. **Provide Summary and Maintenance Tips**
   
   After organizing:
   
   ```markdown
   # Organization Complete! ✨
   
   ## What Changed
   
   - Created [X] new folders
   - Organized [Y] files
   - Freed [Z] GB by removing duplicates
   - Archived [W] old files
   
   ## New Structure
   
   [Show the new folder tree]
   

## Examples

### Example 1: Organizing Downloads (From Justin Dielmann)

**User**: "My Downloads folder is a mess with 500+ files. Help me organize it."

**Process**:
1. Analyzes Downloads folder
2. Finds patterns: work docs, personal photos, installers, random PDFs
3. Proposes structure:
   - Downloads/
     - Work/
     - Personal/
     - Installers/ (DMG, PKG files)
     - Archive/
     - ToSort/ (things needing decisions)
4. Asks for confirmation
5. Moves files intelligently based on content and names
6. Results: 500 files → 5 organized folders

### Example 2: Finding and Removing Duplicates

**User**: "Find duplicate files in my Documents and help me decide which to keep."

**Output**:
```markdown
# Found 23 Sets of Duplicates (156 MB total)

## Duplicate Set 1: "proposal.pdf"
- `/Documents/proposal.pdf` (2.3 MB, modified: 2024-03-15)
- `/Documents/old/proposal.pdf` (2.3 MB, modified: 2024-03-15)
- `/Desktop/proposal.pdf` (2.3 MB, modified: 2024-03-10)

**Recommendation**: Keep `/Documents/proposal.pdf` (most recent in correct location)
Delete the other 2 copies?

[Continue for all duplicates...]
```

### Example 3: Restructuring Projects Folder

**User**: "Review my ~/Projects directory and suggest improvements."

**Output**:
```markdown
# Analysis of ~/Projects

## Current Structure Issues
- Mix of active and archived projects (3+ years old)
- No consistent naming convention
- Some projects at root, others in random subfolders
- Duplicate folders (project-name, project-name-old, project-name-v2)

## Proposed Structure

```
Projects/
├── Active/
│   ├── client-work/
│   ├── side-projects/
│   └── learning/
├── Archive/
│   ├── 2022/
│   ├── 2023/
│   └── 2024/
└── Templates/
```

## Specific Changes
1. Move 12 projects not touched since 2022 → Archive/
2. Consolidate 4 duplicate project folders
3. Rename projects to consistent format: "client-name-project-name"
4. Create Archive for old work

Want me to implement this?
```

### Example 4: Organizing Photos by Date

**User**: "Organize my photo folders by year and month."

**Output**: Creates structure like:
```
Photos/
├── 2023/
│   ├── 01-January/
│   ├── 02-February/
│   └── ...
├── 2024/
│   ├── 01-January/
│   └── ...
└── Unsorted/
```

Then moves photos based on EXIF data or file modification dates.

## Common Organization Tasks

### Downloads Cleanup
```
Organize my Downloads folder - move documents to Documents, 
images to Pictures, keep installers separate, and archive files 
older than 3 months.
```

### Project Organization
```
Review my Projects folder structure and help me separate active 
projects from old ones I should archive.
```

### Duplicate Removal
```
Find all duplicate files in my Documents folder and help me 
decide which ones to keep.
```

### Desktop Cleanup
```
My Desktop is covered in files. Help me organize everything into 
my Documents folder properly.
```

### Photo Organization
```
Organize all photos in this folder by date (year/month) based 
on when they were taken.
```

### Work/Personal Separation
```
Help me separate my work files from personal files across my 
Documents folder.
```

## Best Practices

### Folder Naming
- Use clear, descriptive names
- Avoid spaces (use hyphens or underscores)
- Be specific: "client-proposals" not "docs"
- Use prefixes for ordering: "01-current", "02-archive"

### File Naming
- Include dates: "2024-10-17-meeting-notes.md"
- Be descriptive: "q3-financial-report.xlsx"
- Avoid version numbers in names (use version control instead)
- Remove download artifacts: "document-final-v2 (1).pdf" → "document.pdf"

### When to Archive
- Projects not touched in 6+ months
- Completed work that might be referenced later
- Old versions after migration to new systems
- Files you're hesitant to delete (archive first)

## Related Use Cases

- Setting up organization for a new computer
- Preparing files for backup/archiving
- Cleaning up before storage cleanup
- Organizing shared team folders
- Structuring new project directories