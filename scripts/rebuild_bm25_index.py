from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from modular_ontology.store import rebuild_bm25_index

    parser = argparse.ArgumentParser(description="Backfill the local SQLite FTS5 BM25 document index.")
    parser.add_argument("--db", type=Path, default=None, help="Optional SQLite database path.")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    result = rebuild_bm25_index(args.db, batch_size=args.batch_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
