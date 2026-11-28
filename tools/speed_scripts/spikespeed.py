"""Where Spike's ~40 ms per program go, and whether a precompiled DTB / smaller memory / no commit log change results."""
import os, random, re, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, "/repo/rocket"); sys.path.insert(0, "/repo")
import rv64fuzz

work = Path(tempfile.mkdtemp())
elfs = []
for i in range(30):
    rng = random.Random(9000 + i)
    _, g = rv64fuzz.generator_args(True, "base" if i % 2 else "ext", "rocket")
    asm = rv64fuzz.gen_program(rng, 40, *g)
    d = work / f"p{i}"; d.mkdir()
    elfs.append(rv64fuzz.build_elf(asm, d))

MEM = "-m0x80000000:0x800000,0x60000000:0x1000"
# DTB as spike would generate it: dump it once
dts = subprocess.run(["spike", "--isa=rv64gc", MEM, "--dump-dts", str(elfs[0])], capture_output=True, text=True, env=rv64fuzz.ENV).stdout
(work / "s.dts").write_text(dts)
r = subprocess.run(["dtc", "-O", "dtb", "-o", str(work / "s.dtb"), str(work / "s.dts")], capture_output=True, text=True)
print("dtc:", r.returncode, r.stderr[:200], "dts bytes", len(dts))

def spike(args, elf):
    p = subprocess.run(["spike"] + args + [str(elf)], env=rv64fuzz.ENV, capture_output=True, text=True, timeout=30)
    return [int(m.group(1), 16) for m in re.finditer(r"mem 0x0*60000010 0x([0-9a-f]+)", p.stderr)], p

variants = {
    "current": ["--isa=rv64gc", MEM, "-l", "--log-commits"],
    "commits_only": ["--isa=rv64gc", MEM, "--log-commits"],
    "dtb": ["--isa=rv64gc", MEM, "--dtb=" + str(work / "s.dtb"), "-l", "--log-commits"],
    "dtb_commits_only": ["--isa=rv64gc", MEM, "--dtb=" + str(work / "s.dtb"), "--log-commits"],
}
ref = {}
for name, args in variants.items():
    ts, same = [], 0
    for e in elfs:
        t = time.perf_counter(); v, p = spike(args, e); ts.append(time.perf_counter() - t)
        if name == "current": ref[e] = v
        same += v == ref[e] and bool(v)
    ts.sort()
    print(f"{name:18s} median {1000*ts[len(ts)//2]:6.1f} ms  mean {1000*sum(ts)/len(ts):6.1f} ms  same dumps {same}/{len(elfs)}",
          ("stderr: " + p.stderr[:150].replace(chr(10), " | ")) if not v else "")
# where does time go: spike --help-like trivial start, dtc alone
t = time.perf_counter(); [subprocess.run(["dtc", "-O", "dtb", "-o", "/dev/null", str(work / "s.dts")], capture_output=True) for _ in range(20)]
print("dtc alone %.1f ms" % (1000 * (time.perf_counter() - t) / 20))
print("log lines (current):", len(spike(variants["current"], elfs[0])[1].stderr.splitlines()),
      "(commits_only):", len(spike(variants["commits_only"], elfs[0])[1].stderr.splitlines()))

def tm(args, e, n=10):
    t = time.perf_counter()
    for _ in range(n): subprocess.run(["spike"] + args + [str(e)], env=rv64fuzz.ENV, capture_output=True, timeout=30)
    return 1000 * (time.perf_counter() - t) / n
e = elfs[0]
print("no log           %.1f" % tm(["--isa=rv64gc", MEM], e))
print("small mem log    %.1f" % tm(["--isa=rv64gc", "-m0x80000000:0x100000,0x60000000:0x1000", "-l", "--log-commits"], e))
print("rv64imafd isa    %.1f" % tm(["--isa=rv64imafdc_zicsr_zifencei", MEM, "-l", "--log-commits"], e))
t = time.perf_counter()
for _ in range(10): subprocess.run(["spike", "--help"], capture_output=True)
print("spike --help     %.1f" % (1000 * (time.perf_counter() - t) / 10))
t = time.perf_counter()
for _ in range(10): subprocess.run(["true"], capture_output=True)
print("true             %.1f" % (1000 * (time.perf_counter() - t) / 10))
print(subprocess.run(["bash","-c","ldd $(which spike) | wc -l; ls -la $(which spike); spike --help 2>&1 | head -3"],capture_output=True,text=True).stdout)
import collections
lines = spike(variants["current"], e)[1].stderr.splitlines()
pcs = collections.Counter(l.split()[3] if len(l.split())>3 else "" for l in lines if l.startswith("core"))
print("distinct pcs", len(pcs), "top", pcs.most_common(3))
