from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]


def main():
    print("[1/3] unit + api + isolation + bridge contract")
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT)
    if result.returncode: return result.returncode
    print("[2/3] workspace data contract")
    sys.path.insert(0, str(ROOT / "src"))
    from patent_viewer.domain import Repository
    repo = Repository(ROOT)
    for environment in ("normal", "debug"):
        researches = repo.list_researches(environment)
        if not researches: raise SystemExit(f"{environment}: research fixture missing")
        for research in researches: repo.dashboard(environment, research["id"])
        print(f"  {environment}: {len(researches)} research(es) OK")
    print("[3/3] browser-smoke is run against http://127.0.0.1:8765 after server start")
    print("DEBUG checks passed; always return the UI to NORMAL after browser-smoke.")
    return 0


if __name__ == "__main__": raise SystemExit(main())
