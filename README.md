# notebooklm-manage

Interactive artifact manager for Google NotebookLM.

fzf-based CLI for listing, downloading (with post-processing), and deleting artifacts from NotebookLM notebooks.

## Features

- Interactive notebook / artifact selection via [fzf](https://github.com/junegunn/fzf)
- Parallel download with concurrency control
- Post-processing: audio compression, PDF compression, watermark removal, image resize
- Cross-platform: Windows, macOS, Linux, WSL

## Quick Start

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- [fzf](https://github.com/junegunn/fzf)
- [notebooklm-py](https://github.com/teng-lin/notebooklm-py) authentication (see below)

### Run (uv)

```bash
# Dependencies are auto-installed via PEP 723 inline metadata
uv run manage-artifacts.py
```

### Run (pip)

```bash
pip install notebooklm-py pillow
python manage-artifacts.py
```

### Authentication Setup

```bash
# First time only: install CLI and login via browser
uv tool install notebooklm-py --with playwright
playwright install chromium
notebooklm login
```

This creates `~/.notebooklm/storage_state.json` with your session credentials.

> **Windows note**: If `notebooklm login` fails due to navigation errors,
> you can authenticate on WSL/Linux and copy the credential file:
> ```bash
> cp ~/.notebooklm/storage_state.json /mnt/c/Users/<username>/.notebooklm/
> ```

## Usage

```bash
uv run manage-artifacts.py              # Interactive (full flow)
uv run manage-artifacts.py -l           # List artifacts only
uv run manage-artifacts.py -d           # Dry-run (no API calls)
uv run manage-artifacts.py -n <id>      # Specify notebook ID directly
uv run manage-artifacts.py -c 5         # 5 concurrent downloads
uv run manage-artifacts.py --ascii      # Force ASCII output (no emoji)
uv run manage-artifacts.py --emoji      # Force emoji output
```

### Interactive Flow

1. Select a notebook (fzf single-select)
2. Select artifacts (fzf multi-select, Tab to toggle, Ctrl-A for all)
3. Choose action:
   - Download + Delete (recommended)
   - Download only
   - Delete only (requires confirmation)
4. Parallel execution with progress display

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `ARTIFACT_BACKUP_DIR` | `~/Documents/NotebookArtifacts` (Win) / `~/NotebookArtifacts` (Unix) | Download base directory |
| `NOTEBOOKLM_TIMEOUT` | `90` | API HTTP timeout (seconds) |
| `NO_EMOJI` | - | Set to `1` to force ASCII-only output |

To set `ARTIFACT_BACKUP_DIR` permanently:

```powershell
# Windows (PowerShell)
[Environment]::SetEnvironmentVariable("ARTIFACT_BACKUP_DIR", "D:\NotebookArtifacts", "User")

# Linux / macOS
echo 'export ARTIFACT_BACKUP_DIR=~/NotebookArtifacts' >> ~/.bashrc
```

## Post-Processing

Automatically applied after download. External tools are optional; if missing, the step is skipped gracefully with a log warning.

| Artifact Type | Processing | Required Tool |
|---|---|---|
| Audio | AAC compression (64kbps) | ffmpeg |
| Infographic | Watermark auto-detection + removal, 1/2 resize | Pillow (bundled) |
| Slides (PDF) | Ghostscript compression, per-page watermark removal | ghostscript, pdftoppm |

## Platform-Specific Installation

### Windows

```powershell
# 1. uv (Python package manager)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. fzf (required — interactive selection)
winget install fzf

# 3. Playwright browser (required — for notebooklm login)
uvx --from notebooklm-py --with playwright playwright install chromium

# 4. ghostscript (recommended — PDF compression)
#    Download installer from https://ghostscript.com/releases/gsdnld.html
#    Verify: gswin64c --version

# 5. poppler (recommended — slide watermark removal)
#    Option A: via scoop
irm get.scoop.sh | iex
scoop install poppler
#    Verify: pdftoppm -v

# 6. ffmpeg (optional — audio compression)
winget install ffmpeg
```

> **Restart PowerShell** after installing tools to pick up PATH changes.

### macOS

```bash
brew install fzf
# Recommended:
brew install ghostscript poppler
# Optional:
brew install ffmpeg
```

### Linux / WSL

```bash
sudo apt install fzf
# Recommended:
sudo apt install ghostscript poppler-utils
# Optional:
sudo apt install ffmpeg
```

## Design

See [docs/design.md](docs/design.md) for architecture and technical details.

## License

MIT
