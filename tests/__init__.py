"""
Offline test suite for the MT5 Smart Trading Bot.

These tests exercise the whole pipeline WITHOUT a live MetaTrader5 terminal and
WITHOUT a network connection. They rely only on the Python standard library
(optional third-party packages make them stronger but are never required), so
they can run on a bare Windows 7 Python install.

Run with:
    python -m unittest discover -s tests -v
    python tests/run_all.py

All text is standard ASCII English only.
"""
