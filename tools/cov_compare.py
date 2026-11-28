"""Paired comparison of two Encarsia campaigns run on the same seeds (e.g. coverage feedback vs plain random, or two
simulator builds): per bug and seed, replay-confirmed first detection and programs to it.

  python tools/cov_compare.py <dir A> <suffix A> <dir B> <suffix B>     (suffix: "" for base, "_cov", ...)
"""
import glob, json, os, re, statistics as st, sys


def load(d, sfx):
    conf = json.load(open(os.path.join(d, "confirmations.json")))
    out = {}
    for f in glob.glob(os.path.join(d, f"*_seed?{sfx}.json")):
        n = os.path.basename(f)
        m = re.match(rf"(driver|multiplexer)_(\d+)_seed(\d){re.escape(sfx)}\.json$", n)
        if not m:
            continue
        r = json.load(open(f))
        fd = r.get("first_detection")
        ok = bool(fd) and conf.get(n, {}).get("confirmed", False)
        out[(m.group(1), int(m.group(2)), int(m.group(3)))] = {
            "first": fd["n_programs"] if ok else None, "programs": r["programs"], "seconds": r["seconds"],
            "asm": fd.get("asm") if fd else None, "mutated": fd.get("mutated") if fd else None}
    return out


def main():
    da, sa, db, sb = sys.argv[1:5]
    A, B = load(da, sa), load(db, sb)
    keys = sorted(set(A) & set(B))
    bugs = sorted({(k, d) for k, d, _ in keys})
    det = lambda X, kd: sum(X[(kd[0], kd[1], s)]["first"] is not None for s in range(5) if (kd[0], kd[1], s) in X)
    ba = [b for b in bugs if det(A, b)]; bb = [b for b in bugs if det(B, b)]
    print(f"runs {len(keys)}; bugs detected A {len(ba)} B {len(bb)}; only A {sorted(set(ba) - set(bb))} only B {sorted(set(bb) - set(ba))}")
    print(f"seed detections A {sum(A[k]['first'] is not None for k in keys)} B {sum(B[k]['first'] is not None for k in keys)}")
    pairs = [(A[k]["first"], B[k]["first"]) for k in keys if A[k]["first"] and B[k]["first"]]
    print(f"both detect {len(pairs)} seeds: B earlier {sum(b < a for a, b in pairs)}, later {sum(b > a for a, b in pairs)}, "
          f"same {sum(a == b for a, b in pairs)}; identical first detecting program "
          f"{sum(A[k]['asm'] == B[k]['asm'] for k in keys if A[k]['first'] and B[k]['first'])}")
    ta = sum(A[k]["seconds"] for k in keys) / sum(A[k]["programs"] for k in keys)
    tb = sum(B[k]["seconds"] for k in keys) / sum(B[k]["programs"] for k in keys)
    print(f"seconds per program A {ta:.3f} B {tb:.3f} (B/A {tb / ta:.2f}); total fuzzing seconds A {sum(A[k]['seconds'] for k in keys):.0f} "
          f"B {sum(B[k]['seconds'] for k in keys):.0f}")
    for k in keys:
        if A[k]["first"] != B[k]["first"]:
            print("  differs", k, "A", A[k]["first"], "B", B[k]["first"], "B mutated" if B[k]["mutated"] else "")


if __name__ == "__main__":
    main()
