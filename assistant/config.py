import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

def required(name):
    value = os.getenv(name, '').strip()
    if not value:
        raise ValueError(f'Заполните {name} в {ROOT / ".env"}')
    return value

def data_dir():
    path = Path(os.getenv('DATA_DIR', 'data'))
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path
