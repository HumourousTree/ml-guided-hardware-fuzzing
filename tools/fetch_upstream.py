#!/usr/bin/env python3
"""Fetch pristine upstream PicoRV32 from YosysHQ, pin its commit, and compare it with our golden copy.

  python3 tools/fetch_upstream.py [--commit f00a88c] [--dest hw_design/upstream_picorv32.v]

f00a88c is the PicoRV32 commit used by the Cascade paper (USENIX Security '24, Appendix B), which makes our DUT
configuration directly comparable with theirs.  Needs network + git; writes <dest>.commit with the full hash and sha256.
"""
import argparse, difflib, hashlib, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", default="f00a88c")
    ap.add_argument("--dest", default=str(ROOT / "hw_design" / "upstream_picorv32.v"))
    ap.add_argument("--golden", default=str(ROOT / "hw_design" / "picorv32_golden.sv"))
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "clone", "-q", "https://github.com/YosysHQ/picorv32.git", d], check=True)
        subprocess.run(["git", "-C", d, "checkout", "-q", a.commit], check=True)
        full = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        text = (Path(d) / "picorv32.v").read_text()
    Path(a.dest).write_text(text)
    sha = hashlib.sha256(text.encode()).hexdigest()
    Path(a.dest + ".commit").write_text(f"{full}\n{sha}\n")
    print(f"upstream {full}\nsha256   {sha}\nwritten  {a.dest}")
    g = Path(a.golden).read_text() if Path(a.golden).exists() else None
    if g is None: return print("no golden copy to compare")
    diff = list(difflib.unified_diff(text.splitlines(), g.splitlines(), "upstream", "golden", lineterm="", n=0))
    print("golden == upstream" if not diff else f"golden DIFFERS from upstream ({len([l for l in diff if l[:1] in '+-' and l[:3] not in ('+++','---')])} changed lines):")
    print("\n".join(diff[:60]))


if __name__ == "__main__":
    main()
