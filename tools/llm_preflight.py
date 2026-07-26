from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from patent_viewer.domain import Repository


def main():
    parser=argparse.ArgumentParser(description="LLMを実行せず接続直前条件を診断")
    parser.add_argument("research_id"); parser.add_argument("--environment", choices=("normal","debug"), default="debug")
    args=parser.parse_args(); result=Repository(ROOT).preflight(args.environment,args.research_id)
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["ready"] else 2


if __name__ == "__main__": raise SystemExit(main())
