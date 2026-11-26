"""Bundle the code the Kaggle job needs into a flat folder for a private Kaggle dataset (Kaggle jobs on accounts
without phone verification have no internet, so they cannot clone the repository).

  python rocket/genmodel/kaggle/bundle.py OUT_DIR        then   kaggle datasets create|version -p OUT_DIR
File names encode paths ("rocket__genmodel__train.py" -> rocket/genmodel/train.py); `COMMIT` records the source.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FILES = ["rocket/rv64fuzz.py", "rocket/isa_tokenizer.py", *sorted(str(p.relative_to(ROOT)).replace("\\", "/")
         for p in (ROOT / "rocket/genmodel").glob("*.py")), *sorted(str(p.relative_to(ROOT)).replace("\\", "/")
         for p in (ROOT / "rocket/templates").glob("*.json"))]


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():
        if old.is_file() and old.name != "dataset-metadata.json":
            old.unlink()
    for f in FILES:
        shutil.copyfile(ROOT / f, out / f.replace("/", "__"))
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    (out / "COMMIT").write_text(commit + "\n")
    meta = out / "dataset-metadata.json"
    if not meta.exists():
        meta.write_text(json.dumps({"title": "benchfuzz-code", "id": "subodhdalvi/benchfuzz-code",
                                    "licenses": [{"name": "other"}]}, indent=1))
    print(f"{len(FILES)} files, commit {commit} -> {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
