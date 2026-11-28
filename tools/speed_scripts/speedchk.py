import random, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/repo/rocket")
import rv64fuzz as f
w = Path(tempfile.mkdtemp())
for i in range(6):
    asm = f.gen_program(random.Random(4242 + i), 40, True, ["base", "ext"][i % 2])
    elf = f.build_elf(asm, w); exp = f.run_spike(elf)
    row = []
    for o in (0, 1, 2):
        got, stop = f.run_rocket(Path(f"/root/opt{o}/Vtop_tiny_soc"), elf, 400000)
        d = [j for j, (x, y) in enumerate(zip(got, exp)) if x != y]
        row.append((o, stop, len(got), len(exp), d[:4]))
    print(i, row)
