## attachFileToChannel

Read a file from a local path OR a URL, post it to the Discord channel (so the user can see it — especially images, which render inline), and attach its contents to the current conversation context so you can see/analyze it too.

**Syntax Examples:**

<attachFileToChannel>
https://example.com/myimage.png
</attachFileToChannel>

<attachFileToChannel>
c:\temp\myimage.png
</attachFileToChannel>

<attachFileToChannel>
/Users/rob/myimage.png
</attachFileToChannel>

<attachFileToChannel>
C:\Users\me\notes\report.txt
</attachFileToChannel>

**Parameters:**
- Supply exactly one source per directive — either a URL (`http://`/`https://`) or a local filesystem path.
- Surrounding quotes around Windows paths with spaces are optional and will be stripped.

**Supported file types:**
- **Images** (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`): posted to Discord (rendered inline) AND embedded into your context as visual content.
- **Text files** (`.txt`, `.md`, `.csv`, `.json`, `.xml`, `.yaml`/`.yml`, `.html`, `.log`, `.py`, `.js`, `.c`, `.sh`, `.bat`, `.ps1`, etc.): posted to Discord as a file attachment AND their text contents are fed into your context for analysis.
- **Other binary files** (`.pdf`, `.zip`, `.docx`, audio, video, etc.): posted to Discord as a file attachment. No content is extracted into your context — use other tools (e.g. `<readimage>` for images, `<runcmd>` for shell extraction) if you need the contents.

**Notes:**
- Files are capped at 10 MB (Discord's free-tier upload ceiling). Larger files are rejected with an error message — do not retry with the same source.
- For URLs, the file is downloaded and re-served through Discord so the user sees it in-channel regardless of whether the source URL is embeddable.
- Works on Windows, macOS, and Linux paths alike.
- Use this when the user asks you to "show", "share", "post", "send", or "attach" a file to the chat. For image-only analysis where the user does NOT need to see the file, prefer `<readimage>`.
