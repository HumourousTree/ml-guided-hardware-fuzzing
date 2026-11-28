import random, re, subprocess, sys, tempfile, time, statistics as st
from pathlib import Path
sys.path.insert(0, "/repo/rocket")
import rv64fuzz as f

N = 30
w = Path(tempfile.mkdtemp())
T = {k: [] for k in ("gen", "as", "ld", "spike_l", "spike_nol", "sim0", "sim1", "sim2")}
same_spike = same_sim = 0
sims = {o: Path(f"/root/opt{o}/Vtop_tiny_soc") for o in (0, 1, 2)}
for i in range(N):
    rng = random.Random(4242 + i)
    t = time.time(); asm = f.gen_program(rng, 40, True, ["base", "ext"][i % 2]); T["gen"].append(time.time() - t)
    (w / "p.S").write_text(asm); (w / "link.ld").write_text(f.LINK)
    t = time.time()
    subprocess.run(["riscv64-unknown-elf-as", "-march=rv64imafdc_zicsr", "-o", "p.o", "p.S"], cwd=w, env=f.ENV, check=True, capture_output=True)
    T["as"].append(time.time() - t); t = time.time()
    subprocess.run(["riscv64-unknown-elf-ld", "-T", "link.ld", "-o", "p.elf", "p.o"], cwd=w, env=f.ENV, check=True, capture_output=True)
    T["ld"].append(time.time() - t)
    elf = w / "p.elf"
    t = time.time(); a = f.run_spike(elf); T["spike_l"].append(time.time() - t)
    t = time.time()
    p = subprocess.run(["spike", "--isa=rv64gc", "-m0x80000000:0x800000,0x60000000:0x1000", "--log-commits", str(elf)],
                       env=f.ENV, capture_output=True, text=True, timeout=30)
    T["spike_nol"].append(time.time() - t)
    b = [int(m.group(1), 16) for m in re.finditer(r"mem 0x0*60000010 0x([0-9a-f]+)", p.stderr)] or None
    same_spike += a == b
    outs = []
    for o, sim in sims.items():
        if not sim.exists():
            continue
        t = time.time(); outs.append(f.run_rocket(sim, elf, 400000)); T[f"sim{o}"].append(time.time() - t)
    same_sim += all(x == outs[0] for x in outs) and outs[0][0] == a
for k, v in T.items():
    if v:
        print(f"{k:10s} mean {st.mean(v)*1000:7.1f} ms  median {st.median(v)*1000:7.1f} ms")
print(f"spike without -l gives the same dump: {same_spike}/{N}; all sim builds agree with each other and Spike: {same_sim}/{N}")
