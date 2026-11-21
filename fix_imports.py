"""מתקן את הייבוא הפנימי לעבודה כחבילה: import db -> from app import db"""
import re
from pathlib import Path

APP = Path(__file__).parent / "backend" / "app"
MODULES = ["db", "travel", "confirm", "voice", "scheduler", "policy",
           "agent", "executors", "backup", "prompts"]

targets = list(APP.glob("*.py")) + list((APP / "api").glob("*.py"))
changed = []

for path in targets:
    src = path.read_text(encoding="utf-8")
    original = src
    for mod in MODULES:
        src = re.sub(rf"^import {mod}$", f"from app import {mod}", src, flags=re.M)
        src = re.sub(rf"^from {mod} import ", f"from app.{mod} import ", src, flags=re.M)
    if src != original:
        path.write_text(src, encoding="utf-8")
        changed.append(path.name)

print("תוקנו:", ", ".join(changed) if changed else "כלום")
