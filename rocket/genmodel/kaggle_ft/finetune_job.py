"""Kaggle job: fine-tune the pretrained generator on mutant kills (Track 1, stage 2). Pushed with
  kaggle kernels push -p rocket/genmodel/kaggle_ft --accelerator NvidiaTeslaT4
Inputs: the code + labels dataset (subodhdalvi/benchfuzz-code, rocket/genmodel/kaggle/bundle.py) and the output of
the pretraining job (kernel source subodhdalvi/benchfuzz-genmodel-pretrain). For each pretrained checkpoint: SFT on
pool programs weighted by train-mutant kills, then DPO on (killer, non-killer) pairs; then samples for evaluation
(256 bodies per profile from the pretrained, SFT and DPO models). Only TRAIN mutants are read; dev mutants and the
published Encarsia bugs are left for evaluation on the cloud runners. No secrets, no internet.
"""
import glob
import json
import os
import subprocess
import sys
import time

W = "/kaggle/working"
GEN_LABELS = [f"data/labels/{c}_{k}_labels.jsonl.gz" for c in ("rocket", "boom") for k in ("driver", "multiplexer")]
GEN_POOLS = ["data/labels/rocket_pool.json.gz", "data/labels/boom_pool.json.gz"]
OP1_LABELS = ["data/labels/op1_rocket_labels.jsonl.gz", "data/labels/op1_boom_labels.jsonl.gz"]
OP1_POOLS = ["data/labels/op1_rocket_pool.json.gz", "data/labels/op1_boom_pool.json.gz"]
# ROUND selects the recipe. Each model: init checkpoint (glob under /kaggle/input), sft steps, sft lr, dpo steps,
# dpo lr, SFT weighting, labels, pools, sample tags.
#   r1: run 1 (small) and run 2 (base): SFT + DPO on the generator pools' kills.
#   r2: on-policy round 1: the model's own samples labelled against train mutants no generator program kills
#       (genmodel-onpolicy.yml), merged with the generator labels; kills weighted by rarity. "small_rar" is the
#       ablation without the on-policy data (rarity weighting alone), so the two effects can be told apart.
ROUND = os.environ.get("BF_ROUND", "r2")
RECIPES = {
    "r1": {"base": ("base_pre.pt", 150, 5e-5, 200, 5e-6, "count", GEN_LABELS, GEN_POOLS, ("pre", "sft", "dpo", "dponogram")),
           "small": ("small_pre.pt", 150, 1e-4, 200, 1e-5, "count", GEN_LABELS, GEN_POOLS, ("pre", "sft", "dpo", "dponogram"))},
    "r2": {"small_op1": ("small_pre.pt", 300, 1e-4, 300, 1e-5, "rarity", GEN_LABELS + OP1_LABELS, GEN_POOLS + OP1_POOLS, ("dpo",)),
           "small_rar": ("small_pre.pt", 300, 1e-4, 300, 1e-5, "rarity", GEN_LABELS, GEN_POOLS, ("dpo",))},
}
N_SAMPLES = int(os.environ.get("BF_SAMPLES", "256"))


def run(cmd, log):
    t0 = time.time()
    print("$", " ".join(cmd), flush=True)
    with open(log, "a") as f:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            print(line, end="", flush=True); f.write(line)
        rc = p.wait()
    print(f"[{rc}] {time.time() - t0:.0f} s", flush=True)
    return rc


def code_tree() -> str:
    hits = glob.glob("/kaggle/input/**/rocket__rv64fuzz.py", recursive=True)
    if not hits:
        raise SystemExit("code dataset subodhdalvi/benchfuzz-code is not attached")
    src = os.path.dirname(hits[0])
    for f in os.listdir(src):
        if "__" in f:
            dst = os.path.join("/tmp/code", *f.split("__"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            path = os.path.join(src, f)
            if os.path.isdir(path):              # Kaggle unpacks uploaded .gz files into a directory: re-pack
                import gzip
                inner = [os.path.join(path, x) for x in os.listdir(path)]
                with open(inner[0], "rb") as a, gzip.open(dst + ".gz", "wb") as b:
                    b.write(a.read())
                continue
            with open(path, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
    os.chdir("/tmp/code")
    return open(os.path.join(src, "COMMIT")).read().strip()


def main():
    commit = code_tree()
    py = sys.executable
    import torch
    gpu = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]
    log = f"{W}/console.txt"
    summary = {"commit": commit, "gpus": gpu, "round": ROUND, "models": {}}
    for preset, (init, s_steps, s_lr, d_steps, d_lr, weighting, labels, pools, tags) in RECIPES[ROUND].items():
        pre = glob.glob(f"/kaggle/input/**/{init}", recursive=True)
        missing = [p for p in labels + pools if not os.path.exists(p)]
        if not pre or missing:
            summary["models"][preset] = {"error": f"missing: {init if not pre else missing}"}; continue
        data = f"/tmp/sft_{preset}.npz"
        if run([py, "-m", "rocket.genmodel.data", "sft", "--labels", *labels, "--pool", *pools,
                "--weighting", weighting, "--out", data], log):
            summary["models"][preset] = {"error": "sft data build failed"}; continue
        st = {"pretrained": pre[0], "data": json.load(open(data.replace(".npz", ".json")))}
        sft, dpo = f"{W}/{preset}_sft.pt", f"{W}/{preset}_dpo.pt"
        st["sft_rc"] = run([py, "-m", "rocket.genmodel.train", "sft", "--data", data, "--init", pre[0],
                            "--steps", str(s_steps), "--bs", "16", "--lr", str(s_lr), "--warmup", "20",
                            "--val-frac", "0.05", "--eval-every", "25", "--out", sft], log)
        if st["sft_rc"] == 0:
            st["dpo_rc"] = run([py, "-m", "rocket.genmodel.train", "dpo", "--pairs", data.replace(".npz", ".pairs.json"),
                                "--init", sft, "--steps", str(d_steps), "--bs", "8", "--lr", str(d_lr), "--beta", "0.1",
                                "--eval-every", "25", "--out", dpo], log)
        st["samples"] = {}
        # grammar-constrained samples from every stage; plus unconstrained DPO samples to measure the grammar
        for tag, ck, kill, extra in (("pre", pre[0], False, []), ("sft", sft, True, []), ("dpo", dpo, True, []),
                                     ("dponogram", dpo, True, ["--no-grammar"])):
            if tag not in tags or not os.path.exists(ck):
                continue
            for core, prof in [(c, p) for c in ("rocket", "boom") for p in ("base", "ext", "llmflash")]:
                out = f"{W}/samples_{preset}_{tag}_{core}_{prof}.json"
                run([py, "-m", "rocket.genmodel.sample", "--ckpt", ck, "--core", core, "--profile", prof,
                     "--n", str(N_SAMPLES if not extra else N_SAMPLES // 2), "--seed", "7"]
                    + (["--kill"] if kill else []) + extra + ["--out", out], log)
                if os.path.exists(out):
                    st["samples"][f"{tag}/{core}/{prof}"] = json.load(open(out))["summary"]
        for ck in (sft, dpo):
            lj = ck.replace(".pt", ".log.json")
            if os.path.exists(lj):
                st[os.path.basename(lj)] = json.load(open(lj))[-3:]
        summary["models"][preset] = st
        json.dump(summary, open(f"{W}/summary.json", "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
