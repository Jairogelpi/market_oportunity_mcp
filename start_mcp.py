"""MCP entry point independent of the client's cwd and PYTHONPATH."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if sys.version_info < (3, 10):
    raise SystemExit(f'MarketScope requiere Python >=3.10; recibido {sys.version.split()[0]} en {sys.executable}')
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

def load_dotenv(path):
    """Minimal KEY=VALUE loader for local secrets; variables already in the environment win."""
    if not path.is_file(): return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_dotenv(ROOT / '.env')

from marketscope.mcp_server import main
main()
