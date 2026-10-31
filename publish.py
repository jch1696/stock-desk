"""index.html 에 Supabase URL·anon key 를 넣어 docs/index.html 로 만들고 커밋·푸시(GitHub Pages: main /docs)."""
import os, subprocess, sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, "D:/kiwoom-trade")
from storage.secrets import get_secret
from sync.supabase_client import URL_KEY, KEY_KEY
ROOT = os.path.dirname(os.path.abspath(__file__))
url, key = (get_secret(URL_KEY) or "").strip(), (get_secret(KEY_KEY) or "").strip()
src = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
src = src.replace("const DEFAULT_URL = '';", f"const DEFAULT_URL = '{url}';").replace("const DEFAULT_KEY = '';", f"const DEFAULT_KEY = '{key}';")
os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
open(os.path.join(ROOT, "docs", "index.html"), "w", encoding="utf-8", newline="\n").write(src)
subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
subprocess.run(["git", "commit", "-q", "-m", "publish"], cwd=ROOT)
subprocess.run(["git", "push", "-q"], cwd=ROOT, check=True)
print("✓ 배포 → https://jch1696.github.io/stock-desk/ (빌드 ~1분)")
