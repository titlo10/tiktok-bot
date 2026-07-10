import io
import sys
import tokenize
from pathlib import Path

roots = (Path("atp"), Path("tiktok_bot"), Path("tests"))
violations: list[str] = []
for root in roots:
    for path in root.rglob("*.py"):
        tokens = tokenize.tokenize(io.BytesIO(path.read_bytes()).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                violations.append(f"{path}:{token.start[0]}")

if violations:
    print("\n".join(violations))
    sys.exit(1)
