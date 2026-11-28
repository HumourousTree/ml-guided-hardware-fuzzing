"""(1) programs of every profile except llmtgt are byte-identical to the committed rv64fuzz (git HEAD);
(2) 500 BOOM llmtgt programs: the bug-free BOOM agrees with Spike (calibration as in run_encarsia)."""
import importlib.util, random, subprocess, sys, tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
REPO = Path("/mnt/c/Users/subod/Downloads/ML Guided Hardware Fuzzing_Parent/ML Guided Hardware fuzzing")
sys.path.insert(0, str(REPO / "rocket")); sys.path.insert(0, str(REPO))
import rv64fuzz as new

def load_head():
    src = subprocess.run(["git", "-C", str(REPO), "show", "HEAD:rocket/rv64fuzz.py"], capture_output=True, text=True).stdout
    p = REPO / "rocket" / "_rv64fuzz_head.py"; p.write_text(src)
    spec = importlib.util.spec_from_file_location("rv64fuzz_head", p); m = importlib.util.module_from_spec(spec)
    sys.modules["rv64fuzz_head"] = m; spec.loader.exec_module(m); return m

def cal(i):
    _, g = new.generator_args(True, "llmtgt", "boom")
    asm = new.gen_program(random.Random(424200 + i), 40, *g)
    w = Path(tempfile.mkdtemp()); elf = new.build_elf(asm, w)
    exp = new.run_spike(elf)
    if exp is None:
        return "spike"
    got, stop = new.run_rocket(Path.home() / "boom_runs" / "golden_multiplexer" / "Vtop_tiny_soc", elf, 400000)
    return "ok" if stop and got == exp else ("hang" if not stop else "mismatch")

if __name__ == "__main__":
    old = load_head(); (REPO / "rocket" / "_rv64fuzz_head.py").unlink()
    same = n = 0
    for core in ("rocket", "boom"):
        for prof in ("base", "ext", "llm", "llmflash", "llmctl"):
            for s in range(60):
                _, g1 = new.generator_args(True, prof, core); _, g2 = old.generator_args(True, prof, core)
                a = new.gen_program(random.Random(s), 40, *g1); b = old.gen_program(random.Random(s), 40, *g2)
                n += 1; same += a == b
    print(f"identical programs (other profiles): {same}/{n}", flush=True)
    import collections
    with ProcessPoolExecutor(6) as ex:
        print("BOOM llmtgt calibration:", collections.Counter(ex.map(cal, range(500))))
