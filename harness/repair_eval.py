"""Guarded LLM repair evaluation with independent samples (pass@k) and formal certification, one model per run.

  python3 -m harness.repair_eval [--bugs ...] [--samples 5] [--model gemini-3.8-flash] [--mode informed|blind]

pass@k uses the unbiased estimator of Chen et al. (2021): 1 - C(n-c, k)/C(n, k) from n independent samples with c successes.
Each sample starts from a fresh model session and a cleared rejection memory, with one attempt (no feedback), so samples are
independent.  A sample is
  * gate-accepted: the unchanged pipeline (slice, ddmin, localise, patch parsing, lint, reproducer, growth prefixes) returns a
    candidate and the whole regression suite (seeds + every bug stimulus, memory delays 0 and 1) matches lockstep;
  * correct on held-out: 200 unseen random programs x delays 0 and 3 also match;
  * certified: Yosys whole-core equivalence with the golden RTL is proven (or the candidate is identical).
pass@k is reported for gate acceptance and for certification.  The guarded feedback loop (up to 3 attempts with lint/reproducer
feedback) is one extra run per bug, reported separately because its attempts are not independent.

--mode blind is the reading-vs-reasoning control: the same candidate blocks and gates, but the prompt carries no failing
instruction, reproducer, discrepancy signature or block diagnosis; only "one of these blocks contains an injected defect".

Every evaluation uses a private copy of the buggy RTL (never hw_design/picorv32_mutable.sv), so it can run next to other jobs.
Results are written after every sample and the run resumes where it stopped.  A sample in which the model never answered
(quota, outage) is not counted; the run stops so it can be resumed later.
"""
import argparse
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from unittest import mock

from harness import pico_campaign as pc
from harness.riscv_cosim import build_and_simulate_program, compare_rvfi_trace, get_space_safe_tempdir, prepare_simulator
from harness.template_repair import _held_out, _suite_result, formal_equivalence
from hw_design.bug_injector import GOLDEN_RTL, injected_text, load_manifest


def pass_at_k(n: int, c: int, k: int) -> float:
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


class LLMUnavailable(RuntimeError):
    """The model gave no answer at all during a sample (quota exhausted, outage); the sample must not be scored."""


def blind_prompt(original):
    """Wrap format_localized_repair_prompt so the prompt keeps only the candidate blocks and the task."""
    def build(minimal_reproducer, failure_record, localization, golden_register_state=None):
        text = original(minimal_reproducer, failure_record, localization, golden_register_state)
        text = text.replace("The PicoRV32 RTL disagrees with the golden Python RV32IM reference. The reference is authoritative.",
                            "One of the candidate RTL blocks below contains an injected defect. No failing test, reproducer or "
                            "discrepancy information is provided.")
        text = re.sub(r"=== FAILING INSTRUCTION ===.*?(?==== CANDIDATE RTL BLOCKS)", "", text, flags=re.S)
        # drop each block's diagnosis line (it may describe the failure); keep header and numbered code
        text = re.sub(r"(--- Candidate block \d+: [^\n]*---\n).*?(```verilog)", r"\1\2", text, flags=re.S)
        return text.replace("(the fault is most likely inside one of them)", "")
    return build


def evaluate_bug(bug_id: str, model: str, mode: str, samples: int, feedback_attempts: int, golden: str,
                 regression: list[dict], held_out: list[dict], prior: dict, save) -> dict:
    manifest = load_manifest("all")
    stim = pc._generate_bug_targeted_stimulus(bug_id)
    work = get_space_safe_tempdir(f"llmrepair-{bug_id}-")
    rec = dict(prior or {}, bug_id=bug_id, level=manifest[bug_id]["level"], model=model, mode=mode)
    rec.setdefault("independent", [])
    try:
        rtl = work / "buggy" / "picorv32_buggy.sv"; rtl.parent.mkdir()
        rtl.write_text(injected_text([bug_id]))
        ok, log = prepare_simulator(rtl, work)
        if not ok:
            return dict(rec, error="RTL does not build: " + log[-300:])
        sim = build_and_simulate_program(rtl, stim, work, generate_vcd=False)
        cmp = compare_rvfi_trace(stim, sim.get("hardware_trace", []), hardware_trap=sim.get("hardware_trap", False))
        if not cmp["mismatches"]:
            return dict(rec, detected=False)
        rec["detected"] = True
        cand_dir = work / "verify"; cand_dir.mkdir()
        answers: list[str] = []
        real_generate = pc._generate_with_gemini

        def counting(client, models, prompt, purpose=""):
            text, used, attempts = real_generate(client, models, prompt, purpose=f"{purpose}:{mode}:{bug_id}")
            if text is not None:
                answers.append(used)
            return text, used, attempts

        def one(max_retries: int, tag: str) -> dict:
            pc._GEMINI_CHATS.clear(); pc._GEMINI_TURNS.clear(); pc._REJECTED_NOTES.clear()      # independence
            answers.clear()
            t0 = time.time()
            patches = [mock.patch.object(pc, "HW", rtl), mock.patch.object(pc, "ALLOW_GOLDEN_RECOVERY", False),
                       mock.patch.object(pc, "_generate_with_gemini", counting)]
            if mode == "blind":
                patches.append(mock.patch.object(pc, "format_localized_repair_prompt",
                                                 blind_prompt(pc.format_localized_repair_prompt)))
            for p in patches:
                p.start()
            try:
                cand, src, attempts, info = pc._llm_repair_hardware_with_delta_debug({"mem_delay": 0}, stim, cmp, work,
                                                                                      max_retries=max_retries)
            finally:
                for p in reversed(patches):
                    p.stop()
            if not answers:
                raise LLMUnavailable(f"{model} gave no answer ({src}); stopping so the run can be resumed")
            r = {"tag": tag, "source": src, "attempts": attempts, "answered_by": sorted(set(answers)), "llm_calls": len(answers),
                 "seconds": round(time.time() - t0, 1), "localized": info.get("localized_subsystem"), "status": info.get("status"),
                 "gate_accepted": False, "correct_on_held_out": None, "exact_fix": False, "certified": False}
            if cand and src.startswith("gemini"):
                f = cand_dir / "candidate.sv"; f.write_text(cand)
                passed, total = _suite_result(f, regression, cand_dir, delays=(0, 1))
                r["gate_accepted"] = passed == total
                if r["gate_accepted"]:
                    h_ok, h_tot = _suite_result(f, held_out, cand_dir, delays=(0, 3))
                    r.update(correct_on_held_out=h_ok == h_tot, held_out=f"{h_ok}/{h_tot}",
                             exact_fix=cand.strip() == golden.strip(), formal=formal_equivalence(golden, cand))
                    r["certified"] = r["formal"]["verdict"] in ("identical", "proven")
                    saved = Path("logs/experiments/llm_repair_candidates") / f"{bug_id}__{mode}__{model}__{tag}.sv"
                    saved.parent.mkdir(parents=True, exist_ok=True); saved.write_text(cand)
                    r["candidate_file"] = str(saved)
                shutil.rmtree(cand_dir / "obj_dir", ignore_errors=True)
                (cand_dir / ".build_stamp").unlink(missing_ok=True)                   # force a rebuild for the next candidate
            return r

        while len(rec["independent"]) < samples:
            rec["independent"].append(one(1, f"s{len(rec['independent'])}"))
            save(rec)
        if mode == "informed" and feedback_attempts > 1 and "feedback_run" not in rec:
            rec["feedback_run"] = one(feedback_attempts, "feedback")
            save(rec)
        ind = rec["independent"]
        n, c, cc = len(ind), sum(x["gate_accepted"] for x in ind), sum(x["certified"] for x in ind)
        rec.update(samples=n, gate_accepted=c, certified=cc,
                   **{"pass@1": round(pass_at_k(n, c, 1), 3), "pass@1_certified": round(pass_at_k(n, cc, 1), 3)},
                   **({"pass@5": round(pass_at_k(n, c, 5), 3), "pass@5_certified": round(pass_at_k(n, cc, 5), 3)} if n >= 5 else {}),
                   exact_fix_rate=round(sum(x["exact_fix"] for x in ind) / max(1, n), 3), complete=True)
        return rec
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="pass@k evaluation of guarded LLM repair (one model, resumable)")
    ap.add_argument("--bugs", nargs="*")
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--feedback-attempts", type=int, default=3)
    ap.add_argument("--model", default="gemini-3.8-flash")
    ap.add_argument("--mode", choices=["informed", "blind"], default="informed")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    os.environ["GEMINI_MODEL"], os.environ["GEMINI_FALLBACK_MODEL"] = a.model, ""       # pin the model: no silent fallback
    client, models = pc._get_gemini_client()
    if client is None:
        sys.exit("no Gemini client (GEMINI_API_KEY missing?)")
    out = Path(a.out or f"logs/experiments/llm_repair_{a.mode}_{a.model}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    results = json.loads(out.read_text()) if out.exists() else {}
    golden = GOLDEN_RTL.read_text()
    manifest = load_manifest("all")
    bugs = a.bugs or list(manifest)
    held_out = _held_out()
    stims = {b: pc._generate_bug_targeted_stimulus(b) for b in manifest}
    for b in bugs:
        if results.get(b, {}).get("complete") or results.get(b, {}).get("detected") is False:
            continue
        regression = list(pc.DEFAULT_SEED_PROGRAMS) + [p for k, p in stims.items() if p and k != b] + [stims[b]]

        def save(rec, b=b):
            results[b] = rec
            out.write_text(json.dumps(results, indent=2, default=str))
        try:
            r = evaluate_bug(b, a.model, a.mode, a.samples, a.feedback_attempts, golden, regression, held_out, results.get(b), save)
        except LLMUnavailable as exc:
            print(f"[repair_eval] {exc}", flush=True)
            sys.exit(3)
        save(r)
        if r.get("detected"):
            print(f"{b:40s} gate pass@1={r['pass@1']} pass@5={r.get('pass@5')} | certified pass@1={r['pass@1_certified']} "
                  f"pass@5={r.get('pass@5_certified')} | exact={r['exact_fix_rate']}"
                  + (f" | feedback={r['feedback_run']['certified']}" if "feedback_run" in r else ""), flush=True)
        else:
            print(f"{b:40s} {r.get('error') or 'not detected by its stimulus (skipped)'}", flush=True)


if __name__ == "__main__":
    main()
