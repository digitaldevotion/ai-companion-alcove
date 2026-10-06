---
name: svg-render
description: Renders an SVG file into a styled PNG image using svglib, reportlab, and PyMuPDF (cross-platform, no native C libraries required). Use this skill when SVG content needs to be rasterized into a PNG for display in a Discord channel—such as rendering diagrams, charts, icons, illustrations, or any SVG document for visual review.
---

# SVG Render

## Overview

Render an SVG file into a PNG image via a three-stage pipeline: `svglib` parses the SVG into a ReportLab drawing, `reportlab.graphics.renderPDF` writes that drawing to an intermediate PDF (pure Python, no C extension), and `PyMuPDF` (`fitz`) rasterizes the PDF to a PNG. PyMuPDF ships self-contained binary wheels with its own bundled MuPDF, so no system-level native libraries (cairo, libpng, etc.) are required — this keeps the skill working on macOS, Linux, and Windows, including Python 3.13/3.14 where reportlab's native `renderPM` C extension may not yet have wheels.

The script defensively strips markdown code fences from the input file before parsing, so an LLM that wraps the SVG in fences will not produce invalid SVG. On success the script prints `IMAGE_PATH:<path>` to stdout (along with per-stage `STEP N OK` markers — see Example Trace).

## Dependencies

- **Python 3**
- **svglib** — `pip3 install svglib` (parses SVG into a ReportLab drawing)
- **reportlab** — `pip3 install reportlab` (renders the drawing to an intermediate PDF; pure Python, no C extension involved in this path)
- **PyMuPDF** — `pip3 install pymupdf` (rasterizes the PDF to PNG; ships bundled binary wheels with its own MuPDF for macOS, Linux, and Windows — no system libraries required)

All three install via prebuilt wheels with no native system dependencies, ensuring consistent behavior across macOS, Linux, and Windows.

## Critical Constraints

**YOU MUST perform all four steps in order. Do NOT skip any step. Do NOT collapse steps. Do NOT declare success until STEP 4 has executed.**

1. **NEVER wrap SVG content in markdown code fences** (` ``` ` or ` ```svg ` or ` ```xml `). The `<output>` tool writes content verbatim — any fence characters become part of the file and produce invalid SVG. (The script strips fences defensively as a safety net, but you are still required to write raw SVG.)
2. **ALWAYS use the `<output>` tool** to write the SVG file. Do not use a generic `<write>` tool or inline the SVG into the command.
3. **ALWAYS use the `<runcmd>` tool** to execute the render script. Do not run `python3` via a shell echo or heredoc.
4. **ALWAYS parse the `IMAGE_PATH:` prefix** from the script's stdout before calling `<attachFileToChannel>`. Do not hard-code or guess the output path.
5. **ALWAYS call `<attachFileToChannel>` as the final action.** Rendering the PNG is not sufficient — the user will not see the image until it is attached to the channel.
6. If any `STEP N OK` marker is missing from stdout, or if the script exits non-zero, STOP and report the error. Do not proceed to a later step.

## Workflow

You MUST perform the following four steps in order. Each step lists its action, its expected output, and a common pitfall to avoid.

### STEP 1 — Write the SVG file

Use the `<output>` tool to write the SVG content to a temporary file. Place the **raw** SVG markup directly between the `<output>` tags — **do NOT wrap it in markdown code fences**. A temp path such as `/tmp/svg_render/content.svg` is recommended.

Example call:

```
<output path="/tmp/svg_render/content.svg">
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
  <rect x="10" y="10" width="180" height="180" fill="#12121e" stroke="#64b4ff" stroke-width="2"/>
  <text x="100" y="100" fill="#d2d2e6" font-family="Menlo" font-size="14" text-anchor="middle">Hello</text>
</svg>
</output>
```

**Expected output:** The file exists at the path you specified. No fence characters inside the file.

**Common pitfall:** Wrapping the SVG in ` ```svg ` ... ` ``` ` out of habit. This is the #1 failure mode. The `<output>` tool is not a Markdown renderer — write raw markup only.

### STEP 2 — Execute the render script

Use the `<runcmd>` tool to execute:

```
<python_executable_name> <skill_directory>/scripts/render_svg.py <svg_file_path> [--output <output_path>]
```

The script parses the SVG with `svglib.svg2rlg`, renders a PDF intermediate with `reportlab.graphics.renderPDF`, and rasterizes that PDF to a PNG with `PyMuPDF`. Default output path: `/tmp/svg_render/svg_rendered.png`. The script prints one `STEP N OK` line per stage to stderr and a final `IMAGE_PATH:<path>` line to stdout.

**Expected output (stdout):** Exactly one line of the form `IMAGE_PATH:/tmp/svg_render/svg_rendered.png`.

**Expected output (stderr):** Three lines — `STEP 1 OK`, `STEP 2 OK`, `STEP 3 OK` — confirming each pipeline stage completed. If any of these is missing or replaced by `ERROR:`, stop and report.

**Common pitfall:** Treating the script as a black box and not reading stdout. If `IMAGE_PATH:` is absent, the render failed — do not invent a path.

### STEP 3 — Parse the image path

Parse the `IMAGE_PATH:` prefix from the script's stdout (STEP 2) to retrieve the rendered PNG path. This is the path you will pass to `<attachFileToChannel>` in STEP 4.

**Expected output:** A single absolute filesystem path, e.g. `/tmp/svg_render/svg_rendered.png`.

**Common pitfall:** Hard-coding `/tmp/svg_render/svg_rendered.png` instead of reading it from stdout. If the user passed `--output`, the path will differ.

### STEP 4 — Attach the image to the channel

Use the `<attachFileToChannel>` tool with the path parsed in STEP 3:

```
<attachFileToChannel>
<path_from_step_3>
</attachFileToChannel>
```

**Expected output:** The PNG image appears in the Discord channel.

**Common pitfall:** Stopping after STEP 2. Without this step the user never sees the image. STEP 4 is not optional.

## Common Failure Modes

- **Fences in the SVG file.** The LLM wraps the SVG in ` ```svg ` ... ` ``` `. The script's defensive stripper usually recovers, but you are still required to write raw SVG.
- **Skipping STEP 4.** The LLM renders the PNG and declares success without attaching it to the channel. The user sees nothing.
- **Inventing the image path.** The LLM assumes `/tmp/svg_render/svg_rendered.png` without parsing `IMAGE_PATH:` from stdout. Breaks when `--output` is used.
- **Wrong tool for STEP 1.** The LLM uses a generic `<write>` tool or heredocs the SVG into the shell. Use `<output>` so the file is written verbatim.

## Before Declaring Success

Confirm ALL of the following before reporting completion:

- [ ] STEP 1 executed — SVG file written with raw markup (no fences).
- [ ] STEP 2 executed — script stdout contains `IMAGE_PATH:<path>`.
- [ ] STEP 3 executed — path extracted from `IMAGE_PATH:` prefix.
- [ ] STEP 4 executed — `<attachFileToChannel>` called with that path.

If any box is unchecked, you are not done.

## Example Trace

A complete end-to-end invocation:

**Call 1 — `<output>`:**

```
<output path="/tmp/svg_render/content.svg">
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
  <rect x="10" y="10" width="180" height="180" fill="#12121e" stroke="#64b4ff" stroke-width="2"/>
</svg>
</output>
```

**Call 2 — `<runcmd>`:**

```
python3 /path/to/skills/svg-render/scripts/render_svg.py /tmp/svg_render/content.svg
```

**Stdout (parse this):**

```
IMAGE_PATH:/tmp/svg_render/svg_rendered.png
```

**Stderr (verification markers):**

```
STEP 1 OK
STEP 2 OK
STEP 3 OK
```

**Call 3 — `<attachFileToChannel>`:**

```
<attachFileToChannel>
/tmp/svg_render/svg_rendered.png
</attachFileToChannel>
```

## Script Reference

- `scripts/render_svg.py` — Accepts an SVG file path as input, parses it with `svglib`, renders a PDF intermediate via `reportlab`, and rasterizes it to a PNG via `PyMuPDF`. Defensively strips markdown code fences from the input file before parsing. Optional flag: `--output`/`-o` for custom output path.

  **Exit codes:** `0` = success; `1` = generic/missing-file; `2` = SVG parse failure; `3` = PDF render failure; `4` = rasterize failure.

  **Stdout:** `STEP N OK` per stage (stderr) and a final `IMAGE_PATH:<path>` line (stdout) on success. `ERROR:<message>` on stderr and non-zero exit on failure.
