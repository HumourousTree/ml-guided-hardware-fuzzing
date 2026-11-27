"""Classifier-free guidance on the r4 model: instruction-mix shift per guidance scale.
  python tools/genmodel_cfg_test.py . <dir with small_tgt_{sft,dpo}.pt>"""
import sys, re, time
from pathlib import Path
REPO, FT = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(REPO))
import torch
import rocket.genmodel.data as D
from rocket.genmodel.model import GPT
from rocket.genmodel import grammar
tk = D.tokenizer(); g = grammar.load()
FEAT = {"RVCExpander": lambda m: m.startswith("c."), "MulDiv": lambda m: re.fullmatch(r"(mul|div|rem)\w*", m) is not None,
        "CSRFile": lambda m: m.startswith("csr")}
def share(model, cls, guidance, prof="ext", n=48):
    base = [tk.id["<bos>"], tk.id["ctl:core:rocket"], tk.id[f"ctl:prof:{prof}"], tk.id["ctl:kill"]]
    torch.manual_seed(1)
    outs = model.generate(base + [tk.id[f"ctl:mod:{cls}"]], 400, tk.id["<eos>"], 1.0, 0, n, "cpu",
                          constraints=[grammar.Constraint(g, tk, prof) for _ in range(n)],
                          uncond_prefix=base, guidance=guidance)
    ms = [l.split()[0] for o in outs for l in tk.decode(o).splitlines() if l.split() and not l.split()[0].endswith(":")]
    return round(100 * sum(FEAT[cls](m) for m in ms) / len(ms), 1), round(len(ms) / n, 1)
for ck in ("small_tgt_sft.pt", "small_tgt_dpo.pt"):
    model = GPT.load(FT / ck).eval()
    for cls in FEAT:
        t = time.time()
        row = {gs: share(model, cls, gs) for gs in (0.0, 2.0, 4.0, 8.0)}
        print(f"{ck} {cls:<12} feature share % (lines/body) by guidance:", row, f"{time.time() - t:.0f}s", flush=True)
