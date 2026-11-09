import sys
from pathlib import Path

# Los módulos de app/ se importan entre sí como "from models import ...".
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
