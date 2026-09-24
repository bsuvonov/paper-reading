# paper-reading

A local app for reading research papers, organizing projects, extracting outlines,
and discussing papers with Codex.

Browse conference papers alongside the reader:

![Paper library with conference categories and PDF reader](docs/screenshots/paper-library.png)

Discuss selected papers in the resizable Codex sidebar:

![Codex answering a question beside the paper](docs/screenshots/codex-sidebar.png)

## Get started

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python web_app.py
```

Open **http://127.0.0.1:8000**. If the port is busy, use
`python web_app.py --port 8080`.

## Features

- **Conference libraries:** OSDI, SOSP, NSDI, FAST, USENIX ATC, and USENIX Security,
  organized by year and collapsible session categories.
- **Queued downloads:** browse titles before downloading; save individual papers
  or a selected year. Papers open automatically when ready.
- **Local projects:** create reading collections, import papers by URL or PDF
  upload, and save conference papers to projects.
- **Outlines:** generate section headings and click them to navigate the paper.
  Scanned papers can use local OCR; extracted headings may need review.
- **Reader controls:** resize or hide the sidebar, toggle search, and use dark
  mode while preserving the paper's original colors.

Blocked ACM/SIGOPS downloads fall back to matching public copies, which may be
preprints. Papers without an accessible copy show **No PDF found**; use
**Check again** to retry later. Keep the server running for queued tasks.

## Codex sidebar

Install the [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) and run
`codex login`. The integration uses your local installation and account; use
Linux, macOS, or WSL.

Click the Codex icon, select conferences, years, projects, or individual papers,
then create or resume a session. The paper stays visible beside the terminal.
Use `/model` to change models and `/` to see available commands.

Sessions live in `codex-sessions/`, with links to scoped papers and helpers to
fetch missing papers and extract text. Scope provides research context, not a
security boundary. Retain both this folder and Codex's local history to resume
conversations.

## Command line

```bash
# Download selected OSDI editions; omit --years for all published editions.
python download_osdi.py --years 2024 2025

# Extract outlines and generate outlines/overview.md.
python extract_outlines.py

# Run tests.
python -m unittest discover -s tests
```

Both scripts support `--help`. Rerunning downloads skips verified files.
Older PostScript papers require Ghostscript (`gs`). Avoid running command-line
downloads or extraction alongside app jobs.

OSDI files use `papers/` and `outlines/`; other conferences use `conferences/`,
and local projects use `projects/`. These libraries and Codex sessions are
excluded from Git. The server binds to localhost and is intended for local use.
