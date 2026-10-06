#!/usr/bin/env python3
"""
render_svg.py - Renders an SVG file into a PNG image.

Pipeline: svglib (parse SVG -> ReportLab drawing) -> reportlab renderPDF
(drawing -> PDF, pure Python) -> pymupdf/fitz (PDF -> PNG, bundled binary
wheels, no system C libraries). Cross-platform: macOS, Linux, Windows,
including Python 3.13/3.14 where reportlab's native renderPM C extension
may be unavailable.

A defensive markdown-fence stripper runs first: if the input file was
written by an LLM that wrapped the SVG in ```svg ... ``` fences, the fences
are removed before parsing so svglib sees raw SVG markup.

Verification-friendly stdout/stderr:
  - stderr: one "STEP N OK" line per pipeline stage (parse, pdf, rasterize)
  - stdout: a final "IMAGE_PATH:<path>" line on success
  - On failure: "ERROR:<message>" to stderr and a non-zero exit code

Exit codes:
  0 = success
  1 = generic / missing input file
  2 = SVG parse failure (svglib returned None or raised)
  3 = PDF render failure (reportlab)
  4 = rasterize failure (PyMuPDF) or empty output

Usage: python3 render_svg.py <svg_file_path> [--output <path>]
Output: Prints IMAGE_PATH:<path> on success.
"""

import os
import re
import sys
import argparse
import tempfile

try:
    from svglib.svglib import svg2rlg
except ImportError:
    print(
        "ERROR: svglib is required. Install with: pip3 install svglib",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from reportlab.graphics import renderPDF
except ImportError:
    print(
        "ERROR: reportlab is required. Install with: pip3 install reportlab",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    import pymupdf as fitz  # PyMuPDF (modern import; `fitz` alias is deprecated)
except ImportError:
    try:
        import fitz  # legacy alias, still works on older PyMuPDF versions
    except ImportError:
        print(
            "ERROR: PyMuPDF is required. Install with: pip3 install pymupdf",
            file=sys.stderr,
        )
        sys.exit(1)


EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_PARSE = 2
EXIT_PDF = 3
EXIT_RASTER = 4


_FENCE_OPEN_RE = re.compile(r"^\s*```(?:svg|xml)?\s*$", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")


def strip_markdown_fences(raw):
    """Remove a leading ```svg/```xml/``` line and trailing ``` line if present.

    The <output> tool writes content verbatim. LLMs sometimes wrap code-like
    content in markdown fences; those fence characters would become part of
    the file and produce invalid SVG. This is a safety net that does nothing
    if the content is already raw SVG.
    """
    lines = raw.splitlines()

    # Drop a single leading BOM if present
    if lines and lines[0].startswith("\ufeff"):
        lines[0] = lines[0].lstrip("\ufeff")
        if lines[0] == "":
            lines.pop(0)

    # Strip leading fence
    start = 0
    if lines and _FENCE_OPEN_RE.match(lines[0]):
        start = 1

    # Strip trailing fence
    end = len(lines)
    if end > start and _FENCE_CLOSE_RE.match(lines[end - 1]):
        end -= 1

    cleaned = "\n".join(lines[start:end]).strip()
    # Re-add a trailing newline for tidy XML
    return cleaned + "\n" if cleaned else cleaned


def render_svg(svg_file_path, output_path):
    """Render an SVG file to a PNG via svglib -> reportlab PDF -> PyMuPDF.

    Prints "STEP N OK" markers to stderr as each stage completes.
    Returns the output path. Raises on parse or rasterization failure.
    """
    with open(svg_file_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    cleaned = strip_markdown_fences(raw)
    if not cleaned.strip():
        raise _StageError(EXIT_PARSE, "SVG file is empty after stripping markdown fences.")

    # Stage 1: parse SVG -> ReportLab drawing
    tmp_svg_fd, tmp_svg_path = tempfile.mkstemp(suffix=".svg")
    try:
        with os.fdopen(tmp_svg_fd, "w", encoding="utf-8") as f:
            f.write(cleaned)
        try:
            drawing = svg2rlg(tmp_svg_path)
        except Exception as e:
            raise _StageError(EXIT_PARSE, f"svglib raised while parsing: {e}")
    finally:
        try:
            os.unlink(tmp_svg_path)
        except OSError:
            pass

    if drawing is None:
        raise _StageError(
            EXIT_PARSE,
            "svglib could not parse the SVG file; it may be empty or malformed "
            "(check for stray markdown fences, invalid XML, or unsupported elements).",
        )
    print("STEP 1 OK", file=sys.stderr)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Stage 2: drawing -> PDF (pure Python, no C extension needed)
    tmp_pdf_fd, tmp_pdf_path = tempfile.mkstemp(suffix=".pdf")
    os.close(tmp_pdf_fd)
    try:
        try:
            renderPDF.drawToFile(drawing, tmp_pdf_path)
        except Exception as e:
            raise _StageError(EXIT_PDF, f"reportlab failed to render PDF: {e}")
        print("STEP 2 OK", file=sys.stderr)

        # Stage 3: PDF -> PNG via PyMuPDF (bundled MuPDF, no system libs)
        try:
            doc = fitz.open(tmp_pdf_path)
        except Exception as e:
            raise _StageError(EXIT_RASTER, f"PyMuPDF failed to open intermediate PDF: {e}")
        try:
            if doc.page_count == 0:
                raise _StageError(EXIT_RASTER, "reportlab produced an empty PDF from the SVG.")
            page = doc.load_page(0)
            # Render at 150 DPI (default PDF point = 1/72 inch)
            zoom = 150 / 72
            matrix = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pix.save(output_path)
        except _StageError:
            raise
        except Exception as e:
            raise _StageError(EXIT_RASTER, f"PyMuPDF rasterization failed: {e}")
        finally:
            doc.close()
    finally:
        try:
            os.unlink(tmp_pdf_path)
        except OSError:
            pass

    if not (os.path.isfile(output_path) and os.path.getsize(output_path) > 0):
        raise _StageError(EXIT_RASTER, "Rasterization produced no output file.")
    print("STEP 3 OK", file=sys.stderr)

    return output_path


class _StageError(Exception):
    """Raised with a specific exit code for a pipeline stage failure."""

    def __init__(self, exit_code, message):
        super().__init__(message)
        self.exit_code = exit_code


def main():
    parser = argparse.ArgumentParser(
        description="Render an SVG file into a PNG image."
    )
    parser.add_argument("svg_file", help="Path to SVG file to render")
    parser.add_argument("--output", "-o", default=None, help="Output PNG path")
    args = parser.parse_args()

    if not os.path.isfile(args.svg_file):
        print(f"ERROR: File not found: {args.svg_file}", file=sys.stderr)
        sys.exit(EXIT_GENERIC)

    # Use /tmp on Unix-like systems for a predictable default path; fall back
    # to the platform temp dir on Windows (where /tmp is not standard).
    if os.name == "posix":
        default_tmp = "/tmp"
    else:
        default_tmp = tempfile.gettempdir()
    out = args.output or os.path.join(default_tmp, "svg_render", "svg_rendered.png")

    try:
        render_svg(args.svg_file, out)
    except _StageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(e.exit_code)
    except Exception as e:
        print(f"ERROR: SVG render failed: {e}", file=sys.stderr)
        sys.exit(EXIT_GENERIC)

    print(f"IMAGE_PATH:{out}")


if __name__ == "__main__":
    main()
