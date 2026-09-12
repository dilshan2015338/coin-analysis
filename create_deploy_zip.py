#!/usr/bin/env python3
import os
import zipfile
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ZIP_NAME = "coin_analysis_deploy.zip"
ZIP_PATH = os.path.join(BASE_DIR, ZIP_NAME)

# Exact root files to include
ROOT_FILES = [
    "main.py",
    "bot.py",
    "card_service.py",
    "chart_service.py",
    "gainer_service.py",
    "price_fetcher.py",
    "kline_service.py",
    "config_parser.py",
    "analyst_service.py",
    "db.py",
    "send_test_alert.py",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    ".env.example",
    "README.md",
]

# Directories to include recursively
INCLUDE_DIRS = [
    "assets",
]

# Sensitive or local files to strictly forbid
EXCLUDE_PATTERNS = [
    ".env",
    "alerts.db",
    ".db",
    ".sqlite",
    ".venv",
    "__pycache__",
    ".git",
    ".DS_Store",
    ".pyc",
    "test_pump_preview.png",
    "test_dump_preview.png",
    ZIP_NAME,
]

def should_exclude(rel_path: str) -> bool:
    name = os.path.basename(rel_path)
    if name in (".env", "alerts.db", ".DS_Store"):
        return True
    for pat in EXCLUDE_PATTERNS:
        if pat in rel_path:
            return True
    return False

def create_deploy_archive():
    print(f"📦 Packaging deployment archive: {ZIP_NAME}...")
    files_added = []

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Add individual root files
        for filename in ROOT_FILES:
            file_path = os.path.join(BASE_DIR, filename)
            if os.path.exists(file_path):
                zf.write(file_path, arcname=filename)
                files_added.append((filename, os.path.getsize(file_path)))
            else:
                print(f"⚠️ Warning: Optional file '{filename}' not found, skipping.")

        # 2. Add asset directories recursively
        for d in INCLUDE_DIRS:
            dir_path = os.path.join(BASE_DIR, d)
            if not os.path.exists(dir_path):
                print(f"❌ Error: Required directory '{d}' not found!")
                sys.exit(1)
            for root, dirs, files in os.walk(dir_path):
                for f in sorted(files):
                    abs_f = os.path.join(root, f)
                    rel_f = os.path.relpath(abs_f, BASE_DIR)
                    if not should_exclude(rel_f):
                        zf.write(abs_f, arcname=rel_f)
                        files_added.append((rel_f, os.path.getsize(abs_f)))

    print("\n✅ Deployment Zip Created Successfully!")
    print(f"📁 Output File: {ZIP_PATH}")
    print(f"📊 Total Size: {os.path.getsize(ZIP_PATH) / (1024 * 1024):.2f} MB")
    print("\n📄 Included Files in Archive:")
    for name, size in files_added:
        if size > 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.2f} MB"
        elif size > 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size} B"
        print(f"  • {name:<35} ({size_str})")

    # Safety check
    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        namelist = zf.namelist()
        forbidden = [n for n in namelist if n in (".env", "alerts.db") or n.endswith(".db") or n.endswith(".pyc")]
        if forbidden:
            print(f"\n❌ SECURITY WARNING: Found forbidden files in zip: {forbidden}")
            sys.exit(1)
        else:
            print("\n🔒 Security Check: PASSED (Zero .env secrets or database files included).")

if __name__ == "__main__":
    create_deploy_archive()
