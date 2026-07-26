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
    normal_researches = repo.list_researches("normal")
    for research in normal_researches:
        repo.dashboard("normal", research["id"])
    print(f"  normal: {len(normal_researches)} local research(es) OK")
    debug_researches = repo.list_researches("debug")
    if not debug_researches:
        raise SystemExit("debug: synthetic research fixture missing")
    for research in debug_researches:
        repo.dashboard("debug", research["id"])
    print(f"  debug: {len(debug_researches)} research fixture(s) OK")
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
