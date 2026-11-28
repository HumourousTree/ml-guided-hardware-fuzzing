#!/bin/bash
# Minimise the recoverable first detections of the Rocket coverage campaign (run 46083050852) inside the CI image.
set -eu
cd /repo
IN=/s/cov_rocket; OUT=/s/minimized_rocket.json
python3 /s/minbuild.py rocket '/s/cov_rocket/*_seed?_cov.json' 3
python3 rocket/minimize.py --core rocket --jobs 6 --max-tests 300 --out $OUT $IN/*_seed?_cov.json
python3 - <<'EOF'
import json, statistics as st
d = json.load(open("/s/minimized_rocket.json"))
v = [x for x in d.values() if "skipped" not in x]
print("minimised", len(v), "skipped", len(d) - len(v), {x["skipped"] for x in d.values() if "skipped" in x})
print("body lines: median", st.median(x["body_lines"] for x in v), "->", st.median(x["min_body_lines"] for x in v),
      "max min", max(x["min_body_lines"] for x in v), "confirmed", sum(x["confirmed"] for x in v),
      "1-minimal", sum(bool(x["one_minimal"]) for x in v), "median tests", st.median(x["tests"] for x in v),
      "median s", st.median(x["seconds"] for x in v))
EOF
