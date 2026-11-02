"""PyInstaller entry point for the pgm2mc bundle.

engine/pgm2mc/__main__.py uses a relative import (`from .cli import main`), so it cannot be the top-level
script of a frozen app. This launcher runs it as the package's __main__ module instead, exactly like
`python -m pgm2mc`.
"""
import runpy

if __name__ == "__main__":
    runpy.run_module("pgm2mc", run_name="__main__", alter_sys=True)
