## readSkill

Load a skill's full instruction file from disk into the current conversation context. Use this when a user's request matches the description of an installed skill, so you can read and follow the skill's detailed instructions.

**Syntax Example:**
<readSkill>
/absolute/path/to/skill.md
</readSkill>

**Notes:**
- The path must point to a file inside the `skills/` directory at the project root.
- Symlinks are rejected for security.
- The path is provided in the directive body (one path per directive).
- If the skill file is too large to fit in the available context budget, the load will be aborted and an error returned — do not retry; inform the user.
- After loading, follow the instructions in the skill file to fulfill the user's request.
