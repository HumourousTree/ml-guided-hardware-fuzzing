"""Head-to-head benchmark: every generator runs on the SAME DUT, harness, validator, coverage definition and budget.

Methods (all produce validated RV32IM programs):
  random     - constrained-random regression baseline (stand-in for random regression / TheHuzz-style short programs;
               `--length` scales program length toward Cascade-style long programs)
  mutation   - coverage-guided mutational fuzzing (AFL/RFUZZ/DifuzzRTL-style feedback loop, our implementation)
  semantic   - ChipFuzzer-style LLM generation aimed at uncovered RTL lines (harness.semantic_gen)
  hybrid     - `mutation` until the ChipFuzzer plateau rule fires (gain < 0.1 pp over 100 validated testcases),
               then `semantic` (the "does the LLM help after the plateau?" experiment)
  hybrid-rand- same switch rule but continues with `random` (equal-time control for the plateau experiment)
These are *reimplementations of the strategies*, not the published tools (TheHuzz/Cascade/BMCFuzz/ChipFuzzer need their own
toolchains and target Rocket/BOOM/CVA6); do not label them as such in results.
"""
import argparse
import json
import math
import random
import shutil
import time
from pathlib import Path

from harness.asm import assemble
from harness.riscv_cosim import (analyze_program, build_and_simulate_program, compare_rvfi_trace, parse_verilator_dat,
                                 get_space_safe_tempdir, prepare_simulator, validate_program)


# ── backends ─────────────────────────────────────────────────────────────────

class RunResult:
    def __init__(self, ok, retired, covered, mismatch, error=None, mismatch_info=None):
        self.ok, self.retired, self.covered, self.mismatch, self.error = ok, retired, covered, mismatch, error
        self.mismatch_info = mismatch_info


class VerilatorBackend:
    """Runs a program on the Verilated RTL; covered = coverage points with a non-zero count in *this* run."""

    def __init__(self, rtl_path: Path, workdir: Path, mem_delay: int = 0):
        self.rtl_path, self.workdir, self.mem_delay = Path(rtl_path), Path(workdir), mem_delay
        self.universe: set[str] = set()

    def run(self, program: dict) -> RunResult:
        sim = build_and_simulate_program(self.rtl_path, program, self.workdir, generate_vcd=False, mem_delay=self.mem_delay)
        if not sim["ok"]:
            return RunResult(False, 0, set(), False, sim.get("error"))
        cmp = compare_rvfi_trace(program, sim["hardware_trace"], hardware_trap=sim.get("hardware_trap", False))
        covered: set[str] = set()
        if sim.get("coverage_path"):
            pts = parse_verilator_dat(sim["coverage_path"])
            self.universe |= set(pts)
            covered = {k for k, c in pts.items() if c > 0}
        # per-program artefacts: a 10k-program campaign would otherwise fill /tmp
        for f in (sim.get("coverage_path"), self.workdir / f"{program['id']}.jsonl", self.workdir / f"{program['id']}.hex"):
            if f: Path(f).unlink(missing_ok=True)
        mm = (cmp["mismatches"] or [None])[0]
        info = mm and {"kind": mm.get("kind"), "insn_hex": mm.get("insn_hex"), "insn": mm.get("insn_disasm"),
                       "retire_order": mm.get("retire_order"), "fields": sorted((mm.get("differences") or {}).keys())}
        return RunResult(True, sim["retired"], covered, bool(cmp["mismatches"]), mismatch_info=info)


# ── generators ───────────────────────────────────────────────────────────────

_INTERESTING = [0, 1, -1, 2047, -2048, 0x7FFFFFFF, 0x80000000, 0x55555555, 0xAAAAAAAA, 0x12345678]


class RandomGen:
    name = "random"

    def __init__(self, rng: random.Random, length: int = 20, fence_rate: float = 0.0):
        self.rng, self.length, self.n, self.fence_rate = rng, length, 0, fence_rate

    def _program_text(self) -> str:
        r, L = self.rng, self.length
        out = ["li x31, 0x1000"] + [f"li x{i}, {r.choice(_INTERESTING) if r.random() < .6 else r.getrandbits(32)}" for i in range(1, 9)]
        lab = 0
        while len(out) < L + 9:
            reg = lambda: f"x{r.randrange(1, 12)}"
            if self.fence_rate and r.random() < self.fence_rate:      # off by default: keeps the RV32IM stream unchanged
                out.append("fence"); continue
            k = r.random()
            if k < .22: out.append(f"{r.choice(['add','sub','sll','slt','sltu','xor','srl','sra','or','and'])} {reg()}, {reg()}, {reg()}")
            elif k < .44: out.append(f"{r.choice(['addi','slti','sltiu','xori','ori','andi'])} {reg()}, {reg()}, {r.choice([-2048,-1,0,1,2047,r.randrange(-2048,2048)])}")
            elif k < .50: out.append(f"{r.choice(['slli','srli','srai'])} {reg()}, {reg()}, {r.choice([0,1,15,16,31,r.randrange(32)])}")
            elif k < .60: op = r.choice(["lw", "lh", "lhu", "lb", "lbu"]); a = {"lw": 4, "lh": 2, "lhu": 2}.get(op, 1); out.append(f"{op} {reg()}, {a * r.randrange(0, 0x7FC // a)}(x31)")
            elif k < .70: op = r.choice(["sw", "sh", "sb"]); a = {"sw": 4, "sh": 2}.get(op, 1); out.append(f"{op} {reg()}, {a * r.randrange(0, 0x7FC // a)}(x31)")
            elif k < .80: out.append(f"{r.choice(['mul','mulh','mulhsu','mulhu','div','divu','rem','remu'])} {reg()}, {reg()}, {reg()}")
            elif k < .93:
                lab += 1; out += [f"{r.choice(['beq','bne','blt','bge','bltu','bgeu'])} {reg()}, {reg()}, L{lab}", f"addi {reg()}, {reg()}, {r.randrange(-8,8)}", f"L{lab}:"]
            elif k < .97: out.append(f"{r.choice(['lui','auipc'])} {reg()}, {r.randrange(0, 1 << 20)}")
            else:
                lab += 1; out += [f"jal x{r.randrange(1,12)}, L{lab}", f"addi {reg()}, {reg()}, 1", f"L{lab}:"]
        return "\n".join(out + ["nop"])

    def next(self, state) -> list[dict]:
        for _ in range(20):
            words = assemble(self._program_text())
            if analyze_program(words)["ok"]:
                self.n += 1
                return [{"id": f"random-{self.n}", "base": "0x00000000", "words": [f"0x{w:08x}" for w in words], "src": "random"}]
        return []

    def feedback(self, prog, res, new): pass


class MutationGen:
    name = "mutation"

    def __init__(self, rng: random.Random, length: int = 20):
        self.rng, self.n = rng, 0
        self.seeder = RandomGen(rng, length)
        self.corpus: list[dict] = []          # entries: {'words': [...], 'picked': int}

    def _mutate(self, words: list[int]) -> list[int]:
        r, w = self.rng, list(words)
        for _ in range(r.choice([1, 1, 2, 3])):
            k = r.random()
            if k < .30 and len(w) > 2:   i = r.randrange(len(w) - 1); w[i] ^= 1 << r.randrange(7, 32)                       # bit flip (not opcode)
            elif k < .50 and len(w) > 3: i, j = r.randrange(len(w) - 1), r.randrange(len(w) - 1); w[i], w[j] = w[j], w[i]   # swap
            elif k < .70 and self.corpus:                                                                                   # splice another seed
                other = r.choice(self.corpus)["words"]; cut = r.randrange(1, max(2, len(w))); w = w[:cut] + other[r.randrange(len(other)):][:r.randrange(1, 8)] + w[cut:]
            elif k < .85 and len(w) > 2: i = r.randrange(len(w) - 1); w[i] = (w[i] & 0xFFFFF) | (r.getrandbits(12) << 20) if (w[i] & 0x7F) in (0x13, 0x03) else w[i]
            else:
                src = self.seeder.next(None)
                if src: w = w[:-1] + [int(x, 0) for x in src[0]["words"]][9:-1] + [0x13]
        return w if w[-1] == 0x13 else w + [0x13]

    def next(self, state) -> list[dict]:
        if len(self.corpus) < 4:
            return self.seeder.next(state)
        for _ in range(30):
            weights = [1.0 / (1 + e["picked"]) for e in self.corpus]
            parent = self.rng.choices(self.corpus, weights)[0]; parent["picked"] += 1
            words = self._mutate(parent["words"])
            if analyze_program(words)["ok"]:
                self.n += 1
                return [{"id": f"mut-{self.n}", "base": "0x00000000", "words": [f"0x{x:08x}" for x in words], "src": "mutation"}]
        return self.seeder.next(state)

    def feedback(self, prog, res, new):
        if new > 0 or len(self.corpus) < 4:
            self.corpus.append({"words": [int(x, 0) for x in prog["words"]], "picked": 0})


class SemanticGen:
    name = "semantic"

    def __init__(self, semantic_generator, fallback):
        self.sg, self.fallback, self.n = semantic_generator, fallback, 0

    def next(self, state) -> list[dict]:
        self.n += 1
        executed = self.sg.generate(state["covered"], f"sem-{self.n}")
        # the SemanticGenerator already ran these on the backend; the runner re-runs nothing for them
        return [dict(p, src="semantic", _result=res) for p, res in executed] or self.fallback.next(state)

    def feedback(self, prog, res, new): self.fallback.feedback(prog, res, new)


class HybridGen:
    """first -> second after the plateau rule fires."""

    def __init__(self, first, second, window=100, thresh_pp=0.1, label="hybrid"):
        self.first, self.second, self.window, self.thresh, self.name = first, second, window, thresh_pp, label
        self.switched_at = None

    def next(self, state):
        if self.switched_at is None and plateau_reached(state["pct_history"], self.window, self.thresh):
            self.switched_at = {"programs": state["n_programs"], "t": state["t"]}
        return (self.second if self.switched_at else self.first).next(state)

    def feedback(self, prog, res, new):
        (self.second if self.switched_at else self.first).feedback(prog, res, new)
        if self.switched_at and getattr(self.second, "fallback", None) is not self.first:
            self.first.feedback(prog, res, new)          # keep the first method's corpus learning from the second's finds


# ── metrics ──────────────────────────────────────────────────────────────────

def plateau_reached(pct_history: list[float], window: int = 100, thresh_pp: float = 0.1) -> bool:
    """ChipFuzzer's rule: cumulative coverage gain over `window` consecutive validated testcases < thresh (pp)."""
    return len(pct_history) > window and pct_history[-1] - pct_history[-1 - window] < thresh_pp


def tests_to_threshold(series: list[dict], pct: float):
    return next((s["n_programs"] for s in series if s["pct"] >= pct), None)


def time_to_threshold(series: list[dict], pct: float):
    return next((s["t"] for s in series if s["pct"] >= pct), None)


def coverage_at(series: list[dict], t: float) -> float:
    last = 0.0
    for s in series:
        if s["t"] > t: break
        last = s["pct"]
    return last


def mean_ci95(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n == 0: return float("nan"), float("nan")
    m = sum(xs) / n
    if n == 1: return m, 0.0
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    t = {2: 12.71, 3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36, 9: 2.31, 10: 2.26}.get(n, 1.96 if n > 30 else 2.1)
    return m, t * sd / math.sqrt(n)


def mann_whitney_u(a: list[float], b: list[float]) -> tuple[float, float]:
    """(U, two-sided p) with normal approximation and tie correction; needs >= ~8 per group to be meaningful."""
    na, nb = len(a), len(b)
    allv = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks, i = {}, 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]: j += 1
        for k in range(i, j + 1): ranks[k] = (i + j) / 2 + 1
        i = j + 1
    ra = sum(ranks[k] for k, (_, g) in enumerate(allv) if g == 0)
    u = ra - na * (na + 1) / 2
    n = na + nb; ties = {}
    for v, _ in allv: ties[v] = ties.get(v, 0) + 1
    sigma2 = na * nb / 12 * ((n + 1) - sum(t ** 3 - t for t in ties.values()) / (n * (n - 1)))
    if sigma2 <= 0: return u, 1.0
    z = (u - na * nb / 2) / math.sqrt(sigma2)
    return u, math.erfc(abs(z) / math.sqrt(2))


def unique_points(covered_a: set[str], covered_b: set[str]) -> int:
    return len(covered_a - covered_b)


# ── runner ───────────────────────────────────────────────────────────────────

def page_of(key: str) -> str:
    """Verilator coverage kind of a point key: 'v_line', 'v_toggle', 'v_branch' (or 'other')."""
    for p in ("v_line", "v_toggle", "v_branch"):
        if p in key:
            return p
    return "other"


def run_campaign(backend, gen, *, budget_seconds: float | None = None, budget_programs: int | None = None,
                 stop_on_bug: bool = False, label: str = "", clock=time.monotonic, program_log=None) -> dict:
    """Run `gen` against `backend`; record a time series that is comparable across methods.

    ``program_log``: optional writable text file; every validated program is appended as one JSON line.
    """
    t0 = clock()
    covered: set[str] = set()
    pages: dict[str, int] = {}
    series, n_prog, n_ins, bug, harness_error = [], 0, 0, None, None
    state = {"covered": covered, "universe": backend.universe, "pct_history": [], "n_programs": 0, "t": 0.0}
    while harness_error is None:
        state["t"] = clock() - t0
        if budget_seconds is not None and state["t"] >= budget_seconds: break
        if budget_programs is not None and n_prog >= budget_programs: break
        batch = gen.next(state)
        if not batch: break
        for prog in batch:
            res = prog.pop("_result", None) or backend.run(prog)
            if not res.ok:
                # failed runs do not count against the budget, so a broken build (e.g. a full /tmp) would loop forever
                failures = state.get("consecutive_failures", 0) + 1
                state["consecutive_failures"] = failures
                if failures >= 25:
                    harness_error = res.error
                    break
                continue
            state["consecutive_failures"] = 0
            fresh = res.covered - covered
            new = len(fresh)
            for k in fresh:
                pages[page_of(k)] = pages.get(page_of(k), 0) + 1
            covered |= res.covered
            n_prog += 1; n_ins += res.retired
            pct = 100.0 * len(covered & backend.universe) / max(1, len(backend.universe))
            state["pct_history"].append(pct); state["n_programs"] = n_prog
            series.append({"t": round(clock() - t0, 4), "n_programs": n_prog, "n_instructions": n_ins, "covered": len(covered),
                           "pct": round(pct, 4), "new_points": new, "src": prog.get("src", gen.name), "pages": dict(pages)})
            if program_log is not None:
                program_log.write(json.dumps({"n": n_prog, "words": prog.get("words")}) + "\n")
            gen.feedback(prog, res, new)
            if res.mismatch and bug is None:
                bug = {"program": prog.get("id"), "n_programs": n_prog, "n_instructions": n_ins, "t": round(clock() - t0, 4),
                       "words": prog.get("words"),      # kept so the failure can be re-run and delta-debugged later
                       "mismatch": getattr(res, "mismatch_info", None)}
                if stop_on_bug: break
        if stop_on_bug and bug: break
    universe_pages: dict[str, int] = {}
    for k in backend.universe:
        universe_pages[page_of(k)] = universe_pages.get(page_of(k), 0) + 1
    return {"label": label or gen.name, "method": gen.name, "series": series, "universe": len(backend.universe),
            "harness_error": harness_error,
            "universe_pages": universe_pages,
            "bug_detected": bug, "switched_at": getattr(gen, "switched_at", None),
            "covered_points": sorted(covered), "wall_seconds": round(clock() - t0, 3)}


def main():
    ap = argparse.ArgumentParser(description="BenchFuzz head-to-head coverage / bug benchmark")
    ap.add_argument("--methods", nargs="+", default=["random", "mutation", "hybrid"], choices=["random", "mutation", "semantic", "hybrid", "hybrid-rand"])
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--budget-seconds", type=float, default=600)
    ap.add_argument("--budget-programs", type=int, default=None)
    ap.add_argument("--length", type=int, default=20)
    ap.add_argument("--rtl", default="hw_design/picorv32_golden.sv")
    ap.add_argument("--bug", default=None, help="inject this bug id (suite 'all') and stop at first detection")
    ap.add_argument("--out", default="logs/bench")
    ap.add_argument("--seed-start", type=int, default=0, help="first seed index (run disjoint ranges in parallel)")
    ap.add_argument("--mem-delay", type=int, default=0, help="max random memory wait states (same for every method)")
    ap.add_argument("--fence-rate", type=float, default=0.0, help="probability of a FENCE per generated slot (Cascade P6 study)")
    ap.add_argument("--stop-on-mismatch", action="store_true", help="stop at the first mismatch even without --bug")
    ap.add_argument("--save-programs", action="store_true", help="write every validated program to <out>/<run>.programs.jsonl")
    a = ap.parse_args()

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    if a.bug:
        # private copy of the buggy RTL: never touches picorv32_mutable.sv, so --bug runs can go in parallel
        from hw_design.bug_injector import injected_text
        text = injected_text([a.bug])
        assert text is not None, f"cannot inject {a.bug}"
        rtl_copy = out / "rtl" / f"{a.bug}_s{a.seed_start}.sv"; rtl_copy.parent.mkdir(exist_ok=True); rtl_copy.write_text(text)
        a.rtl = str(rtl_copy)
    rtl_text = Path(a.rtl).read_text()
    llm_fn = None
    if any(m in ("semantic", "hybrid") for m in a.methods):
        from harness.pico_campaign import _get_gemini_client, _generate_with_gemini
        client, models = _get_gemini_client()
        if client is None: raise SystemExit("semantic/hybrid need GEMINI_API_KEY (or run only random/mutation)")
        llm_fn = lambda prompt: _generate_with_gemini(client, models, prompt, purpose="semantic_generation")[0]
    for seed in range(a.seed_start, a.seed_start + a.seeds):
        for m in a.methods:
            work = get_space_safe_tempdir(f"bench-{m}-{seed}-")
            backend = VerilatorBackend(a.rtl, work, mem_delay=a.mem_delay)
            prepare_simulator(Path(a.rtl), work)            # one-off Verilator build stays outside the timed region
            rng = random.Random(1000 + seed)
            rnd, mut = RandomGen(rng, a.length, a.fence_rate), MutationGen(random.Random(2000 + seed), a.length)
            mut.seeder.fence_rate = a.fence_rate
            if m == "random": gen = rnd
            elif m == "mutation": gen = mut
            else:
                from harness.semantic_gen import SemanticGenerator
                sem = SemanticGen(SemanticGenerator(rtl_text, backend, llm_fn, random.Random(3000 + seed)), mut)
                gen = sem if m == "semantic" else HybridGen(mut, sem if m == "hybrid" else rnd, label=m)
            stem = f"{m}_{'bug-' + a.bug + '_' if a.bug else ''}seed{seed}"
            plog = open(out / f"{stem}.programs.jsonl", "w") if a.save_programs else None
            try:
                res = run_campaign(backend, gen, budget_seconds=a.budget_seconds, budget_programs=a.budget_programs,
                                   stop_on_bug=bool(a.bug) or a.stop_on_mismatch, label=f"{m}", program_log=plog)
            finally:
                if plog: plog.close()
            res.update(seed=seed, bug=a.bug, rtl=a.rtl, mem_delay=a.mem_delay, length=a.length,
                       budget_seconds=a.budget_seconds, budget_programs=a.budget_programs, fence_rate=a.fence_rate)
            (out / f"{stem}.json").write_text(json.dumps(res))
            print(f"[{m} seed {seed}] programs={len(res['series'])} final={res['series'][-1]['pct'] if res['series'] else 0:.2f}% "
                  f"bug={res['bug_detected']} t={res['wall_seconds']}s", flush=True)
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
