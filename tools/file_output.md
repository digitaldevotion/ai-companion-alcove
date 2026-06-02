## output

Write content to a specified file when requested by the user.

**Syntax Example:**

<output path="/path/to/file.txt">
Your content here
multiple lines
etc.
</output>

**Parameters:**
- **path** (required) — Absolute or relative file path, specified in the path attribute

**Notes:**
- Call this tool when specifically asked to CREATE a file by the user. Keep the output inline unless user specifically asks
- By default, files should be saved to the user's desktop folder
- NEVER ATTEMPT TO OVERWRITE AN EXISTING FILE!

