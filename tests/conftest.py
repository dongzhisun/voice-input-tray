import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ASSETS = ROOT / "assets"
MODEL_DIR = ASSETS / "models" / "sensevoice-int8-2024-07-17"
WAV_DIR = ASSETS / "test_wavs"
