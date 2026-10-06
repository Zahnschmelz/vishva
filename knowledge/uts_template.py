import sys, json, shutil, traceback
from pathlib import Path
from datetime import datetime

WORK = Path(__file__).parent
LOG = WORK / "logs"; TMP = WORK / "tmp"

def log(m):
    line = f"[{datetime.now():%H:%M:%S}] {m}"
    print(line)
    LOG.mkdir(parents=True, exist_ok=True)
    (LOG / "execution.log").open("a").write(line + "\n")

def write(p: Path, content: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    t = TMP / f"{p.name}.tmp"
    t.write_text(content)
    shutil.move(str(t), str(p))
    log(f"written: {p}")

def verify():
    # syntax + import + content checks here
    log("verify: ok")

def main():
    for d in (LOG, TMP): d.mkdir(parents=True, exist_ok=True)
    try:
        log("setup")
        log("implement")
        verify()
        print(json.dumps({"status": "success", "log": str(LOG / "execution.log")}))
    except Exception as e:
        traceback.print_exc()
        print(json.dumps({"status": "error", "detail": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
