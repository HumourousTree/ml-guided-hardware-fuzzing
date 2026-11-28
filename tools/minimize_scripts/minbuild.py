import sys, json, glob
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, "/repo/rocket")
import cloud_shard as cs, run_encarsia as re_

def main():
    core, pattern = sys.argv[1], sys.argv[2]
    names = set()
    for f in glob.glob(pattern):
        r = json.load(open(f))
        if r.get("first_detection"):
            names |= {f"{r['kind']}_{r['dir']}", re_.golden_name(core, r["kind"], r["dir"])}
    print("building", sorted(names), flush=True)
    with ProcessPoolExecutor(int(sys.argv[3]) if len(sys.argv) > 3 else 3) as ex:
        list(ex.map(cs.build_one, [core] * len(names), sorted(names)))
    print("built", flush=True)

if __name__ == "__main__":
    main()
