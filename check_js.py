"""Extract <script> blocks from frontend/index.html and syntax-check them with node. Local helper only."""
import re
import subprocess
import sys
import tempfile

html = open("frontend/index.html", encoding="utf-8").read()
blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
print(f"{len(blocks)} ta script blok topildi")
ok = True
for i, block in enumerate(blocks, 1):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(block)
        path = f.name
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    if r.returncode != 0:
        ok = False
        print(f"Blok {i}: XATO\n{r.stderr}")
    else:
        print(f"Blok {i}: OK")
sys.exit(0 if ok else 1)
