"""Writes the data model diagram into app/database/README.md, coloured by table kind.

paracelsus draws the tables from models.py; this adds one colour per prefix (dim_, fact_, ...).
    python -m app.database.diagram           # rewrite the diagram
    python -m app.database.diagram --check   # exit 1 if it is out of date
"""

import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
README = BACKEND / "app" / "database" / "README.md"
BEGIN, END = "<!-- BEGIN_SQLALCHEMY_DOCS -->", "<!-- END_SQLALCHEMY_DOCS -->"

# Same colours as the stores in the pipeline diagram (README.md).
KINDS = {
    "dim": "fill:#ccfbf1,stroke:#0f766e,stroke-width:2px,color:#1f2937",
    "fact": "fill:#ffe4e6,stroke:#be123c,stroke-width:2px,color:#1f2937",
    "bridge": "fill:#f1f5f9,stroke:#475569,stroke-width:2px,color:#1f2937",
    "ops": "fill:#ffffff,stroke:#94a3b8,stroke-dasharray:4 3,color:#1f2937",
}


def render() -> str:
    cli = Path(sys.executable).parent / "paracelsus"
    graph = subprocess.run([str(cli), "graph", "--layout", "elk"], cwd=BACKEND,
                           capture_output=True, text=True, check=True).stdout.strip()
    tables = re.findall(r"^  (\w+) \{$", graph, flags=re.M)
    lines = [graph, ""]
    for kind, style in KINDS.items():
        names = [t for t in tables if t.startswith(kind + "_")]
        if names:
            lines += [f"  classDef {kind} {style}", f"  class {','.join(names)} {kind}"]
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def main() -> int:
    text = README.read_text()
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    new = text[:start] + "\n" + render() + "\n" + text[end:]
    if "--check" in sys.argv:
        return 0 if new == text else 1
    README.write_text(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
