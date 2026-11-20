# PyInstaller spec for building suplex as a single binary.
# Run: pyinstaller suplex.spec

import os
import sys
from pathlib import Path

block_cipher = None

# Find the package source.
src = Path("src/suplex")

# Data files to bundle (geo data, skills, linkedin autocomplete).
datas = [
    (str(src / "geo" / "us-cities.json"), "suplex/geo"),
    (str(src / "geo" / "worldcities.csv"), "suplex/geo"),
    (str(src / "geo" / "linkedin-location-autocomplete.json"), "suplex/geo"),
    (str(src / "skills"), "suplex/skills"),
]

# Hidden imports that PyInstaller can't detect.
hiddenimports = [
    "suplex.cli",
    "suplex.mcp_server",
    "suplex.scraper.coordinator",
    "suplex.scraper.gmaps_scraper",
    "suplex.scraper.linkedin_scraper",
    "suplex.scraper.site_scraper",
    "suplex.scraper.apify_scraper",
    "suplex.scraper.review_scraper",
    "suplex.enrich.pipeline",
    "suplex.verify.verifier",
    "suplex.store.db",
    "suplex.geo.tiles",
    "suplex.geo.seed",
    "suplex.geo.build_geo_map",
    "suplex.config",
    "suplex.setup",
    "suplex.profile",
    "suplex.jobs",
    "suplex.apify_budget",
    "suplex.models",
    "suplex.ai.strategist",
    # Dependencies that need explicit imports.
    "anyio",
    "anyio._backends._asyncio",
    "httpx",
    "selectolax",
    "selectolax.lexbor",
    "trafilatura",
    "extruct",
    "mcp",
    "click",
    "pydantic",
    "apify_client",
    "psutil",
    "email_validator",
]

a = Analysis(
    [str(src / "__main__.py")],
    pathex=[str(Path("src"))],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="suplex",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
