import sys
import os

# Ensure backend/ root is on sys.path so all imports resolve from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
