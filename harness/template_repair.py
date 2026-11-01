"""Non-LLM drivers for the guarded repair loop: guard validation and a template-repair baseline.

Every driver talks to the *unchanged* guarded pipeline (`pico_campaign._llm_repair_hardware_with_delta_debug`: slice,
ddmin, localise, `// REPLACE LINES` parsing, block-range check, Verilator lint with feedback retries, reproducer
re-verification, grow-prefix verification) through a scripted stand-in for the Gemini client, and then through the
campaign's acceptance rule (the whole regression suite must match, no regression).  A held-out set of random programs,
never seen by any gate, measures whether an accepted patch is actually correct (overfitting check).

Drivers ("models"):
  oracle        - returns the golden text of the hunks that lie inside the offered blocks (positive control: the gates
                  must accept a correct fix; it also bounds what *any* model could achieve given the localisation)
  lint-oracle   - first a syntactically broken version of the oracle patch, then the oracle (lint-retry path)
  noop          - returns the offered lines unchanged (negative control: the reproducer gate must reject it)
  template      - CirFix-style search: generic single-line operator templates (operator swap, negation removal,
                  ternary collapse, operand drop, concatenation-element drop, $signed wrap) applied to the offered lines
                  ranked by Ochiai spectrum suspiciousness from per-program Verilator line coverage

  python3 -m harness.template_repair --models oracle lint-oracle noop template --budget 60
"""
import argparse
import difflib
import json
import math
import random
import re
import shutil
import time
from pathlib import Path
from unittest import mock

from harness import pico_campaign as pc
from harness.benchmark import RandomGen
from harness.riscv_cosim import (build_and_simulate_program, compare_rvfi_trace, get_space_safe_tempdir,
                                 parse_verilator_dat, prepare_simulator)
from hw_design.bug_injector import GOLDEN_RTL, injected_text, load_manifest

_BLOCK_HDR = re.compile(r"\(lines (\d+)-(\d+)\)")
_NUMBERED = re.compile(r"^\s*(\d+): (.*)$")


# ── scripted client (same interface the pipeline uses for google.genai) ──────

class _Reply:
    def __init__(self, text: str):
        self.text = text


class ScriptedClient:
    """`client.chats.create(model=...).send_message(prompt).text == responder(prompt)`."""

    def __init__(self, responder):
        self.responder, self.calls, self.chats = responder, 0, self

    def create(self, model=None):
        return self

    def send_message(self, prompt: str) -> _Reply:
        self.calls += 1
        return _Reply(self.responder(prompt) or "no patch")


def _fmt(patches: list[tuple[int, int, str]]) -> str:
    return "```verilog\n" + "\n".join(f"// REPLACE LINES {a}-{b}\n{code}" for a, b, code in patches) + "\n```"


def _offered_blocks(prompt: str) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in _BLOCK_HDR.findall(prompt)]


def _offered_lines(prompt: str) -> dict[int, str]:
    return {int(m.group(1)): m.group(2) for m in map(_NUMBERED.match, prompt.splitlines()) if m}


# ── oracle / controls ────────────────────────────────────────────────────────

def golden_hunks(buggy: str, golden: str) -> list[tuple[int, int, str]]:
    """(first, last, replacement) hunks (1-based lines of `buggy`) that turn `buggy` into `golden`."""
    b, g = buggy.splitlines(), golden.splitlines()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, g, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2 or j1 == j2:          # pure insertion / deletion: widen by the preceding (unchanged) line
            i1, j1 = i1 - 1, j1 - 1
        out.append((i1 + 1, i2, "\n".join(g[j1:j2])))
    return out


def oracle_responder(golden: str, buggy_rtl: Path, broken_first: bool = False):
    state = {"n": 0}

    def respond(prompt: str) -> str:
        state["n"] += 1
        blocks = _offered_blocks(prompt)
        hunks = [h for h in golden_hunks(buggy_rtl.read_text(), golden) if any(lo <= h[0] and h[1] <= hi for lo, hi in blocks)]
        if not hunks:
            return "I cannot find the defect in the offered blocks."
        if broken_first and state["n"] == 1:
            a, b, code = hunks[0]
            hunks[0] = (a, b, code.replace(";", "", 1) + " @@")     # syntax error -> Verilator lint must reject
        return _fmt(hunks)
    return respond


def noop_responder(prompt: str) -> str:
    lines = _offered_lines(prompt)
    n = min(lines) if lines else 1
    return _fmt([(n, n, lines.get(n, ""))])


# ── template operators ───────────────────────────────────────────────────────

_OPEN, _CLOSE = "([{", ")]}"


def _rhs_start(line: str) -> int | None:
    """Index just after the assignment operator (`=` or `<=`) of a statement, or None."""
    for i, ch in enumerate(line):
        if ch == "=" and line[i + 1:i + 2] != "=" and line[i - 1:i] not in ("=", "!", ">"):
            return i + 1
    return None


def _depths(s: str) -> list[int]:
    d, out = 0, []
    for ch in s:
        if ch in _CLOSE: d -= 1
        out.append(d)
        if ch in _OPEN: d += 1
    return out


def _binary_ops(s: str):
    """(index, op) of binary operators &, |, ^, &&, ||, ==, != in s (reduction operators excluded)."""
    i = 0
    while i < len(s):
        two = s[i:i + 2]
        if two in ("&&", "||", "==", "!="):
            yield i, two; i += 2; continue
        if s[i] in "&|^" and s[i + 1:i + 2] not in ("&", "|", "="):
            prev = s[:i].rstrip()[-1:] if s[:i].strip() else ""
            if prev and (prev.isalnum() or prev in "_)]}'"):
                yield i, s[i]
        i += 1


def _groups(s: str):
    """(start, end) spans of every bracket group's interior plus the whole string."""
    stack, spans = [], [(0, len(s))]
    for i, ch in enumerate(s):
        if ch in _OPEN: stack.append(i)
        elif ch in _CLOSE and stack: spans.append((stack.pop() + 1, i))
    return spans


def _split_top(s: str, sep: str) -> list[str] | None:
    """Split s at depth-0 occurrences of the binary operator (or ',') `sep`; None if there is nothing to split."""
    dep = _depths(s)
    cuts = [i for i, op in _binary_ops(s) if op == sep and dep[i] == 0] if sep != "," else \
           [i for i, ch in enumerate(s) if ch == "," and dep[i] == 0]
    if not cuts:
        return None
    bounds = [0] + [c + len(sep) for c in cuts]
    return [s[a:(cuts[k] if k < len(cuts) else len(s))] for k, a in enumerate(bounds)]


def template_variants(line: str) -> list[str]:
    """Single-line candidate edits from generic, bug-agnostic repair templates."""
    code, _, comment = line.partition("//")
    start = _rhs_start(code)
    end = code.rfind(";")
    if start is None or end < start:
        return []
    head, rhs, tail = code[:start], code[start:end], code[end:] + (("//" + comment) if comment else "")
    out: list[str] = []
    swaps = {"&": "|^", "|": "&^", "^": "&|", "&&": ["||"], "||": ["&&"], "==": ["!="], "!=": ["=="]}
    for i, op in _binary_ops(rhs):                                                  # T1 operator swap
        for new in swaps[op]:
            out.append(rhs[:i] + new + rhs[i + len(op):])
    for m in re.finditer(r"[!~](?!=)", rhs):                                        # T2 negation removal
        out.append(rhs[:m.start()] + rhs[m.end():])
    dep = _depths(rhs)
    for q, ch in enumerate(rhs):                                                    # T3 ternary collapse
        if ch != "?":
            continue
        d = dep[q]
        lo = max([i + 1 for i in range(q) if rhs[i] in _OPEN and dep[i] == d - 1] or [0])
        colon = next((i for i in range(q + 1, len(rhs)) if rhs[i] == ":" and dep[i] == d), None)
        if colon is None:
            continue
        hi = next((i for i in range(colon + 1, len(rhs)) if rhs[i] in _CLOSE + "," and dep[i] == d - 1), len(rhs))
        for branch in (rhs[q + 1:colon], rhs[colon + 1:hi]):
            out.append(rhs[:lo] + " " + branch.strip() + rhs[hi:])
    for a, b in _groups(rhs):                                                       # T4/T5 operand drop, T6 element drop
        inner = rhs[a:b]
        for sep in ("&&", "||", "&", "|", "^") + ((",",) if a > 0 and rhs[a - 1] == "{" else ()):
            parts = _split_top(inner, sep)
            if parts:
                for k in range(len(parts)):
                    keep = parts[:k] + parts[k + 1:]
                    out.append(rhs[:a] + sep.join(keep).strip() + rhs[b:])
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*(<|>)\s*([A-Za-z_]\w*)\b", rhs):       # T7 signed comparison
        out.append(rhs[:m.start()] + f"$signed({m.group(1)}) {m.group(2)} $signed({m.group(3)})" + rhs[m.end():])
    seen, res = {rhs}, []
    for r in out:
        r = re.sub(r"\s+", " ", r).rstrip() if r.strip() else r
        if r not in seen and r.strip():
            seen.add(r); res.append(head.rstrip() + " " + r.strip() + tail)
    return res


# ── spectrum-based fault localisation ────────────────────────────────────────

def _line_hits(dat_path: str) -> set[int]:
    hit = set()
    for key, cnt in parse_verilator_dat(dat_path).items():
        if cnt <= 0 or "v_line" not in key:
            continue
        kv = dict(i.split("\x02", 1) for i in key.split("\x01") if "\x02" in i)
        for part in kv.get("S", kv.get("l", "")).split(","):
            if "-" in part:
                a, b = part.split("-"); hit.update(range(int(a), int(b) + 1))
            elif part.isdigit():
                hit.add(int(part))
    return hit


def ochiai_scores(rtl: Path, failing: list[dict], passing: list[dict], workdir: Path) -> dict[int, float]:
    spectra = []
    for prog, failed in [(p, True) for p in failing] + [(p, False) for p in passing]:
        sim = build_and_simulate_program(rtl, prog, workdir, generate_vcd=False)
        if sim.get("coverage_path"):
            spectra.append((_line_hits(sim["coverage_path"]), failed))
    F = sum(f for _, f in spectra)
    lines = set().union(*(h for h, _ in spectra)) if spectra else set()
    score = {}
    for ln in lines:
        ef = sum(1 for h, f in spectra if f and ln in h)
        ep = sum(1 for h, f in spectra if not f and ln in h)
        score[ln] = ef / math.sqrt(F * (ef + ep)) if F and ef else 0.0
    return score


def template_responder(scores: dict[int, float], log: list[dict]):
    """Yields one single-line candidate per call, offered lines in descending suspiciousness."""
    state = {"queue": None}

    def respond(prompt: str) -> str:
        if log and "Your previous patch was rejected" in prompt:
            reason = prompt.split("Your previous patch was rejected:", 1)[1].strip().splitlines()[0]
            log[-1]["rejected_by"] = ("lint" if reason.startswith("Verilator lint") else
                                      "reproducer" if "minimal reproducer" in reason else
                                      "grow_prefix" if reason.startswith("regression on") else
                                      "patch_format" if reason.startswith("patch error") else reason[:40])
        if state["queue"] is None:
            lines = _offered_lines(prompt)
            # Verilator has no line points for continuous assignments; SBFL cannot exonerate such a line, so it inherits
            # the highest score of its block and wins ties against lines that passing runs were seen to execute
            eff = {}
            for lo, hi in _offered_blocks(prompt):
                top = max([scores[n] for n in range(lo, hi + 1) if n in scores] or [0.0])
                for n in range(lo, hi + 1):
                    if n in lines:
                        eff[n] = max(eff.get(n, 0.0), scores.get(n, top))
            ranked = sorted(lines, key=lambda n: (-eff.get(n, 0.0), n in scores, n))
            state["queue"] = [(n, v) for n in ranked for v in template_variants(lines[n])]
            log.append({"meta": True, "offered_lines": len(lines), "candidates": len(state["queue"])})
        if not state["queue"]:
            return "no more candidates"
        n, text = state["queue"].pop(0)
        log.append({"line": n, "suspiciousness": round(scores.get(n, -1.0), 3), "patch": text.strip()[:160]})
        return _fmt([(n, n, text)])
    return respond


# ── formal equivalence (patch certification) ─────────────────────────────────

def formal_equivalence(golden: str, candidate: str, timeout_s: int = 300) -> dict:
    """Yosys structural+inductive equivalence of the whole core in the harness configuration.

    Returns {"verdict": "identical" | "proven" | "not_proven", "seconds"}.  "not_proven" covers both a real difference and
    a timeout; only "proven"/"identical" certify a patch.  Simulation-based gates cannot do this: an accepted patch that
    corrupts the product only for rare operands passes every random program (see paper, RQ5).
    """
    if candidate.strip() == golden.strip():
        return {"verdict": "identical", "seconds": 0.0}
    import subprocess, tempfile
    d = Path(tempfile.mkdtemp(prefix="equiv-"))
    (d / "gold.v").write_text(golden); (d / "gate.v").write_text(candidate)
    params = " ".join(f"-set {k} {v}" for k, v in __import__("harness.riscv_cosim", fromlist=["HW_PARAMS"]).HW_PARAMS.items())
    prep = f"chparam {params} picorv32; hierarchy -top picorv32; proc; flatten; memory -nomap; memory_map; opt -purge; async2sync"
    script = (f"read_verilog {d}/gold.v; {prep}; rename picorv32 gold; design -stash gold; design -reset; "
              f"read_verilog {d}/gate.v; {prep}; rename picorv32 gate; design -stash gate; design -reset; "
              "design -copy-from gold -as gold gold; design -copy-from gate -as gate gate; "
              "equiv_make gold gate equiv; hierarchy -top equiv; equiv_struct; equiv_simple; equiv_induct; equiv_status -assert")
    t0 = time.time()
    try:
        r = subprocess.run(["yosys", "-q", "-p", script], capture_output=True, text=True, timeout=timeout_s)
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
    shutil.rmtree(d, ignore_errors=True)
    return {"verdict": "proven" if ok else "not_proven", "seconds": round(time.time() - t0, 1)}


# ── evaluation ───────────────────────────────────────────────────────────────

def _suite_result(rtl: Path, programs: list[dict], workdir: Path, delays=(0,)) -> tuple[int, int]:
    ok = 0
    for prog in programs:
        for d in delays:
            sim = build_and_simulate_program(rtl, prog, workdir, generate_vcd=False, mem_delay=d)
            ok += bool(sim["ok"] and compare_rvfi_trace(prog, sim["hardware_trace"], hardware_trap=sim.get("hardware_trap", False))["ok"])
    return ok, len(programs) * len(delays)


def _held_out(n: int = 200) -> list[dict]:
    gen = RandomGen(random.Random(777), 24)
    return [p for _ in range(n) for p in gen.next(None)]


def evaluate(bug_id: str, model: str, budget: int, golden: str, regression: list[dict], held_out: list[dict]) -> dict:
    stim = pc._generate_bug_targeted_stimulus(bug_id)
    work = get_space_safe_tempdir(f"trepair-{bug_id}-")
    t0 = time.time()
    try:
        # private buggy copy: never touches hw_design/picorv32_mutable.sv, so evaluations can run in parallel
        rtl = work / "buggy" / "picorv32_buggy.sv"; rtl.parent.mkdir()
        text = injected_text([bug_id]); assert text is not None, bug_id
        rtl.write_text(text)
        ok, log = prepare_simulator(rtl, work)
        if not ok:
            return {"bug_id": bug_id, "model": model, "error": "does not build"}
        sim = build_and_simulate_program(rtl, stim, work, generate_vcd=False)
        cmp = compare_rvfi_trace(stim, sim.get("hardware_trace", []), hardware_trap=sim.get("hardware_trap", False))
        if not cmp["mismatches"]:
            return {"bug_id": bug_id, "model": model, "detected": False}
        trace: list[dict] = []
        if model == "oracle": responder = oracle_responder(golden, rtl)
        elif model == "lint-oracle": responder = oracle_responder(golden, rtl, broken_first=True)
        elif model == "noop": responder = noop_responder
        else:
            passing = [p for p in regression if p["id"] != stim["id"]][:40]
            responder = template_responder(ochiai_scores(rtl, [stim], passing, work / "sbfl"), trace)
        client = ScriptedClient(responder)
        remaining, attempts_total, gate_rejections, accepted, info = budget if model == "template" else 3, 0, 0, None, {}
        with mock.patch.object(pc, "_get_gemini_client", lambda: (client, [model])), \
             mock.patch.object(pc, "ALLOW_GOLDEN_RECOVERY", False), \
             mock.patch.object(pc, "HW", rtl):
            while remaining > 0:
                pc._GEMINI_CHATS.clear(); pc._GEMINI_TURNS.clear()
                cand, src, attempts, info = pc._llm_repair_hardware_with_delta_debug({"mem_delay": 0}, stim, cmp, work, max_retries=remaining)
                attempts_total += attempts; remaining -= max(1, attempts)
                if not cand or not src.startswith("gemini"):
                    break
                f = work / "accept" / "candidate.sv"; f.parent.mkdir(exist_ok=True); f.write_text(cand)
                passed, total = _suite_result(f, regression, f.parent, delays=(0, 1))
                if passed == total:
                    accepted = cand
                    break
                gate_rejections += 1                              # strict-improvement / no-regression gate
                if trace: trace[-1]["rejected_by"] = "suite_gate"
                if model != "template":
                    break
        rec = {"bug_id": bug_id, "model": model, "detected": True, "accepted": accepted is not None,
               "llm_calls": client.calls, "pipeline_attempts": attempts_total, "suite_gate_rejections": gate_rejections,
               "localized_subsystem": info.get("localized_subsystem"), "localized_lines": info.get("localized_lines"),
               "minimal_length": info.get("minimal_program_length"), "status": info.get("status"),
               "seconds": round(time.time() - t0, 1)}
        if accepted:
            f = work / "accept" / "candidate.sv"
            h_ok, h_tot = _suite_result(f, held_out, f.parent, delays=(0, 3))
            saved = Path("logs/experiments/repair_candidates") / f"{bug_id}__{model}.sv"
            saved.parent.mkdir(parents=True, exist_ok=True); saved.write_text(accepted)
            rec.update(held_out_pass=h_ok, held_out_total=h_tot, correct_on_held_out=h_ok == h_tot,
                       exact_fix=accepted.strip() == golden.strip(), candidate_file=str(saved),
                       formal=formal_equivalence(golden, accepted),
                       diff=[l for l in difflib.unified_diff(golden.splitlines(), accepted.splitlines(), lineterm="", n=0)
                             if l[:1] in "+-" and l[:3] not in ("+++", "---")][:12])
        if model == "template":
            meta = next((t for t in trace if t.get("meta")), {})
            tried = [t for t in trace if not t.get("meta")]
            rec.update(offered_lines=meta.get("offered_lines"), candidate_pool=meta.get("candidates"), candidates_tried=len(tried),
                       rejections={k: sum(t.get("rejected_by") == k for t in tried) for k in
                                   ("patch_format", "lint", "reproducer", "grow_prefix", "suite_gate")},
                       accepted_patch=tried[-1] if accepted and tried else None)
        return rec
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="guard validation + template-repair baseline for the guarded repair loop")
    ap.add_argument("--models", nargs="+", default=["oracle", "lint-oracle", "noop", "template"])
    ap.add_argument("--bugs", nargs="*")
    ap.add_argument("--budget", type=int, default=60, help="template: max candidates per bug")
    ap.add_argument("--out", default="logs/experiments/repair_baselines.json")
    ap.add_argument("--resume", action="store_true", help="skip bug/model pairs already present in --out")
    a = ap.parse_args()
    golden = GOLDEN_RTL.read_text()
    bugs = a.bugs or list(load_manifest("all"))
    regression = list(pc.DEFAULT_SEED_PROGRAMS) + [p for p in (pc._generate_bug_targeted_stimulus(b) for b in load_manifest("all")) if p]
    held_out = _held_out()
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    results = json.loads(out.read_text()) if out.exists() else {}
    for b in bugs:
        for m in a.models:
            if a.resume and f"{b}|{m}" in results:
                continue
            r = evaluate(b, m, a.budget, golden, [p for p in regression if p["id"] != f"bug-targeted-{b}"] + [pc._generate_bug_targeted_stimulus(b)], held_out)
            results[f"{b}|{m}"] = r
            out.write_text(json.dumps(results, indent=2, default=str))
            print(f"{b:40s} {m:12s} accepted={r.get('accepted')} correct={r.get('correct_on_held_out')} exact={r.get('exact_fix')} "
                  f"calls={r.get('llm_calls')} tried={r.get('candidates_tried')} {r.get('seconds')}s", flush=True)


if __name__ == "__main__":
    main()
