# Project Management

Compare two deployment project folders (Prod vs UAT): config files, libraries, and startup/shutdown scripts.

## Location

Main tool: [`compare_Projects/`](compare_Projects/)

## Quick start

```bash
cd compare_Projects
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux:   source .venv/bin/activate
pip install -r requirements.txt
cp config.ini.example config.ini
# Edit config.ini with project paths and SSH settings
python compare_projects.py
```

Reports are written under `compare_Projects/logs/`.
