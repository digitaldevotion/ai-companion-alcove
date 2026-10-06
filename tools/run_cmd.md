## runcmd

Execute one or more operating system commands.

**Syntax Example:**

<runcmd>
echo "hello world"
ls -la /some/path
</runcmd>

**Optional attributes:**

- `timeout` — maximum seconds to wait before killing the command. Defaults to `180` (3 minutes). Maximum allowed is `900` (15 minutes); larger values are clamped. Quotes are required.

**Example with timeout:**

<runcmd timeout="300">
./long_running_build.sh
</runcmd>


**Notes:**
- Timeout is optional and defaults to 180 seconds. Max is 900 seconds
- Commands run sequentially, one per line
- Standard shell syntax applies
- The chosen timeout is reported to the user when execution starts
- NEVER EXECUTE A DESTRUCTIVE CALL WITHOUT FIRST CONSULTING WITH THE USER!
