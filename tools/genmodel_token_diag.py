"""Does the r4 model use the location token?  python tools/genmodel_token_diag.py . <dir with small_tgt_{sft,dpo}.pt>
1. log p(body | mod=C) - log p(body | mod=other) on C's train killers vs on random pool bodies (SFT and DPO checkpoints)
2. rvc share of 64 samples per condition from the SFT checkpoint."""
import sys, json, gzip, random, re, collections
from pathlib import Path
REPO, FT = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(REPO))
import torch
import rocket.genmodel.data as D
from rocket.genmodel.model import GPT
from rocket.genmodel import grammar
tk = D.tokenizer(); loc = D.load_locations()
pool = json.loads(gzip.open(REPO / "data/labels/rocket_pool.json.gz", "rt").read())
bodies = D.pool_bodies(pool)
killers = collections.defaultdict(set)
for k in ("driver", "multiplexer"):
    for l in gzip.open(REPO / f"data/labels/rocket_{k}_labels.jsonl.gz", "rt"):
        r = json.loads(l)
        if r.get("split") == "train" and r.get("killed"):
            killers[D.mod_token(loc.get(("rocket", k, int(r["mutant"]))))].add(r["program"])
prof = {p["id"]: D._profile(p["profile"]) for p in pool["programs"]}
rng = random.Random(0)
def lp(model, mt, pid):
    x = torch.tensor([D.encode(tk, "rocket", prof[pid], bodies[pid], True, mt)[:1024]])
    m = torch.ones_like(x, dtype=torch.float32); m[:, :6] = 0          # body tokens only
    return model.logprob(x, m).item()
for ck in ("small_tgt_sft.pt", "small_tgt_dpo.pt"):
    model = GPT.load(FT / ck).eval()
    with torch.no_grad():
        for C in ("ctl:mod:RVCExpander", "ctl:mod:CSRFile", "ctl:mod:MulDiv"):
            ks = rng.sample(sorted(killers[C]), min(20, len(killers[C]))); rs = rng.sample(sorted(bodies), 20)
            dk = sum(lp(model, C, p) - lp(model, "ctl:mod:other", p) for p in ks) / len(ks)
            dr = sum(lp(model, C, p) - lp(model, "ctl:mod:other", p) for p in rs) / len(rs)
            print(f"{ck} {C}: mean logp gain of the class token on its killers {dk:+.2f}, on random bodies {dr:+.2f} nats")
model = GPT.load(FT / "small_tgt_sft.pt").eval(); g = grammar.load()
def rvc_share(mt, n=48):
    prefix = [tk.id["<bos>"], tk.id["ctl:core:rocket"], tk.id["ctl:prof:ext"], tk.id["ctl:kill"]] + ([tk.id[mt]] if mt else [])
    torch.manual_seed(1)
    outs = model.generate(prefix, 400, tk.id["<eos>"], 1.0, 0, n, "cpu", constraints=[grammar.Constraint(g, tk, "ext") for _ in range(n)])
    ms = [l.split()[0] for o in outs for l in tk.decode(o).splitlines() if l.split() and not l.split()[0].endswith(":")]
    return round(100 * sum(m.startswith("c.") for m in ms) / len(ms), 1)
print("SFT rvc share (ext): none", rvc_share(None), "| RVCExpander", rvc_share("ctl:mod:RVCExpander"), "| other", rvc_share("ctl:mod:other"))
