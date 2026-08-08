"""Make src/ importable for the test package (used by `python -m unittest`)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
