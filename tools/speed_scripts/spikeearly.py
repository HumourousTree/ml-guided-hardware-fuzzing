"""Early-stopped run_spike vs the full Spike run: identical dumps on many programs (base, ext, checkpointed), and time."""
import os, random, re, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, "/repo/rocket"); sys.path.insert(0, "/repo")
import rv64fuzz


def old_spike(elf, timeout=30):
    p = subprocess.run(["spike", "--isa=rv64gc", "-m0x80000000:0x800000,0x60000000:0x1000", "-l", "--log-commits", str(elf)],
                       env=rv64fuzz.ENV, capture_output=True, text=True, timeout=timeout)
    vals = [int(m.group(1), 16) for m in re.finditer(r"mem 0x0*60000010 0x([0-9a-f]+)", p.stderr)]
    return vals or None


N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
same = diff = none = 0
to, tn = [], []
for i in range(N):
    core = ("rocket", "boom")[i % 2]
    prof = ("base", "ext")[(i // 2) % 2]
    os.environ["BENCHFUZZ_CHECKPOINT"] = "1" if i % 5 == 0 else "0"
    _, g = rv64fuzz.generator_args(True, prof, core)
    rng = random.Random(77000 + i)
    asm = rv64fuzz.gen_program(rng, 40, *g)
    w = Path(tempfile.mkdtemp())
    elf = rv64fuzz.build_elf(asm, w)
    t = time.perf_counter(); a = old_spike(elf); to.append(time.perf_counter() - t)
    t = time.perf_counter(); b = rv64fuzz.run_spike(elf); tn.append(time.perf_counter() - t)
    if a == b:
        same += 1; none += a is None
    else:
        diff += 1; print("DIFF", i, core, prof, None if a is None else len(a), None if b is None else len(b))
med = lambda x: 1000 * sorted(x)[len(x) // 2]
print(f"programs {N}: same {same} (both None {none}), diff {diff}; median old {med(to):.1f} ms, new {med(tn):.1f} ms, "
      f"mean old {1000*sum(to)/N:.1f}, new {1000*sum(tn)/N:.1f}")
