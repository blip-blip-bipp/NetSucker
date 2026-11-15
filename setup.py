"""
py2app build script for NetSucker.

Build with:
    ./venv/bin/python3 setup.py py2app

Produces a self-contained dist/NetSucker.app that bundles Python and all
dependencies (rumps, pyobjc), so it runs without the venv or system Python.
"""
from setuptools import setup

APP = ["bandwidth_menubar.py"]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "NetSucker",
        "CFBundleDisplayName": "NetSucker",
        "CFBundleIdentifier": "com.fab.netsucker",
        "LSUIElement": True,  # menu-bar-only app: no Dock icon, no app menu
        "LSBackgroundOnly": False,
    },
}

setup(
    app=APP,
    name="NetSucker",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
