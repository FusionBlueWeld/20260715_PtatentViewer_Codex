import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]


def main():
    parser = argparse.ArgumentParser(description="PatentViewer DEBUG verification suite")
    parser.add_argument(
        "--allow-browser-skip",
        action="store_true",
        help="allow the visible-browser smoke check to be skipped explicitly",
    )
    args = parser.parse_args()

    print("[1/4] unit + api + isolation + bridge contract")
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT)
    if result.returncode: return result.returncode
    print("[2/4] workspace data contract")
    sys.path.insert(0, str(ROOT / "src"))
    from patent_viewer.domain import Repository
    repo = Repository(ROOT)
    for environment in ("normal", "debug"):
        researches = repo.list_researches(environment)
        if not researches: raise SystemExit(f"{environment}: research fixture missing")
        for research in researches: repo.dashboard(environment, research["id"])
        print(f"  {environment}: {len(researches)} research(es) OK")
    print("[3/4] responsive screenshot regression")
    visual = subprocess.run([sys.executable, "-X", "utf8", "tools/ui_visual_check.py"], cwd=ROOT)
    if visual.returncode: return visual.returncode
    print("[4/4] visible-browser semantic block smoke")
    smoke_command = [sys.executable, "-X", "utf8", "tools/browser_smoke.py"]
    if args.allow_browser_skip:
        smoke_command.append("--allow-skip")
    smoke = subprocess.run(smoke_command, cwd=ROOT)
    if smoke.returncode: return smoke.returncode
    print("DEBUG checks passed; browser environments and filters were restored.")
    return 0


if __name__ == "__main__": raise SystemExit(main())
