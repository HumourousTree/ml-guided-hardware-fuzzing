"""Kaggle job: pretrain the program-body generator (Track 1, stage 1). Pushed with
  kaggle kernels push -p rocket/genmodel/kaggle --accelerator NvidiaTeslaT4
It clones the public repository, builds the pretraining corpus from the existing generators, trains a small model
(pipeline check) and then the base model (~85M), samples bodies for every core x profile, and leaves checkpoints,
logs and samples in /kaggle/working (downloaded with `kaggle kernels output`). No secrets are used.
"""
import json
import os
import subprocess
import sys
import time

REPO = "https://github.com/HumourousTree/ml-guided-hardware-fuzzing.git"
BRANCH = "v0.4-evaluation"
W = "/kaggle/working"
N_SEQ = int(os.environ.get("BF_N", "400000"))
STAGES = [("small", 6000, 32, 1e-3), ("base", 8000, 32, 3e-4)]


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


def main():
    os.chdir(W)
    if not os.path.isdir("repo"):
        subprocess.run(["git", "clone", "--depth", "1", "-b", BRANCH, REPO, "repo"], check=True)
    os.chdir("repo")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    py = sys.executable
    summary = {"commit": commit, "n_sequences": N_SEQ, "stages": {}}
    log = f"{W}/console.txt"
    if run([py, "-m", "rocket.genmodel.data", "pretrain", "--n", str(N_SEQ), "--out", "/tmp/pre.npz"], log):
        raise SystemExit("data build failed")
    summary["data"] = json.load(open("/tmp/pre.json"))
    for preset, steps, bs, lr in STAGES:
        ck = f"{W}/{preset}_pre.pt"
        rc = run([py, "-m", "rocket.genmodel.train", "pretrain", "--data", "/tmp/pre.npz", "--preset", preset,
                  "--steps", str(steps), "--bs", str(bs), "--lr", str(lr), "--warmup", "300", "--eval-every", "250",
                  "--out", ck], log)
        st = {"rc": rc}
        if os.path.exists(ck.replace(".pt", ".log.json")):
            st["log_tail"] = json.load(open(ck.replace(".pt", ".log.json")))[-3:]
        if rc == 0:
            st["samples"] = {}
            for core in ("rocket", "boom"):
                for prof in ("base", "ext", "llmflash"):
                    out = f"{W}/samples_{preset}_{core}_{prof}.json"
                    run([py, "-m", "rocket.genmodel.sample", "--ckpt", ck, "--core", core, "--profile", prof,
                         "--n", "128", "--out", out], log)
                    if os.path.exists(out):
                        st["samples"][f"{core}/{prof}"] = json.load(open(out))["summary"]
        summary["stages"][preset] = st
        json.dump(summary, open(f"{W}/summary.json", "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
