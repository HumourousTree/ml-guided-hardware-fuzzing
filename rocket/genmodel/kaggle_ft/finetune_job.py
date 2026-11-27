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
# preset: (sft steps, sft lr, dpo steps, dpo lr)
PLAN = {"base": (150, 5e-5, 200, 5e-6), "small": (150, 1e-4, 200, 1e-5)}
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
    summary = {"commit": commit, "gpus": gpu, "models": {}}
    if run([py, "-m", "rocket.genmodel.data", "sft", "--labels", "data/labels/rocket_driver_labels.jsonl.gz",
            "data/labels/rocket_multiplexer_labels.jsonl.gz", "data/labels/boom_driver_labels.jsonl.gz",
            "data/labels/boom_multiplexer_labels.jsonl.gz", "--pool", "data/labels/rocket_pool.json.gz",
            "data/labels/boom_pool.json.gz",
            "--out", "/tmp/sft.npz"], log):
        raise SystemExit("sft data build failed")
    summary["data"] = json.load(open("/tmp/sft.json"))
    for preset, (s_steps, s_lr, d_steps, d_lr) in PLAN.items():
        pre = glob.glob(f"/kaggle/input/**/{preset}_pre.pt", recursive=True)
        if not pre:
            summary["models"][preset] = {"error": "no pretrained checkpoint"}; continue
        st = {"pretrained": pre[0]}
        sft, dpo = f"{W}/{preset}_sft.pt", f"{W}/{preset}_dpo.pt"
        st["sft_rc"] = run([py, "-m", "rocket.genmodel.train", "sft", "--data", "/tmp/sft.npz", "--init", pre[0],
                            "--steps", str(s_steps), "--bs", "16", "--lr", str(s_lr), "--warmup", "20",
                            "--val-frac", "0.05", "--eval-every", "25", "--out", sft], log)
        if st["sft_rc"] == 0:
            st["dpo_rc"] = run([py, "-m", "rocket.genmodel.train", "dpo", "--pairs", "/tmp/sft.pairs.json", "--init", sft,
                                "--steps", str(d_steps), "--bs", "8", "--lr", str(d_lr), "--beta", "0.1",
                                "--eval-every", "25", "--out", dpo], log)
        st["samples"] = {}
        # grammar-constrained samples from every stage; plus unconstrained DPO samples to measure the grammar
        for tag, ck, kill, extra in (("pre", pre[0], False, []), ("sft", sft, True, []), ("dpo", dpo, True, []),
                                     ("dponogram", dpo, True, ["--no-grammar"])):
            if not os.path.exists(ck):
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
