# Coverage feedback on Rocket (bug-free reference of driver bug 1081), local Docker run with the CI image, 2026-11-28

Scripts: tools/coverage_scripts/ (run inside the encarsia-env image, repo at /repo).

| metric | profile | mutate_p | seed | programs | covered | corpus | mutated | asm fail | ref disagree |
|---|---|---|---|---|---|---|---|---|---|
| line | base | 0.0 | 0 | 150 | 13587 | 10 | 0 | 0 | 0 |
| line | base | 0.0 | 1 | 150 | 13587 | 9 | 0 | 0 | 0 |
| line | base | 0.7 | 0 | 150 | 13587 | 12 | 99 | 0 | 0 |
| line | base | 0.7 | 1 | 150 | 13587 | 10 | 105 | 0 | 0 |
| line | ext | 0.0 | 0 | 150 | 13631 | 12 | 0 | 0 | 0 |
| line | ext | 0.0 | 1 | 150 | 13626 | 8 | 0 | 0 | 0 |
| line | ext | 0.7 | 0 | 150 | 13626 | 13 | 96 | 0 | 0 |
| line | ext | 0.7 | 1 | 150 | 13626 | 11 | 103 | 0 | 0 |
| toggle1 | base | 0.0 | 0 | 300 | 11334 | 62 | 0 | 0 | 0 |
| toggle1 | base | 0.0 | 1 | 300 | 11329 | 52 | 0 | 0 | 0 |
| toggle1 | base | 0.7 | 0 | 300 | 11333 | 65 | 199 | 0 | 0 |
| toggle1 | base | 0.7 | 1 | 300 | 11323 | 71 | 207 | 0 | 0 |
| toggle1 | ext | 0.0 | 0 | 300 | 11428 | 63 | 0 | 0 | 0 |
| toggle1 | ext | 0.0 | 1 | 300 | 11416 | 66 | 0 | 0 | 0 |
| toggle1 | ext | 0.7 | 0 | 300 | 11405 | 62 | 204 | 0 | 0 |
| toggle1 | ext | 0.7 | 1 | 300 | 11413 | 64 | 200 | 0 | 0 |

Coverage-build caveat (feasibility/debug, covdbg2.sh): the line-coverage build of the bug-free core sets spurious FP
exception flags in fcsr on 7 of 8 random programs (plain build and Spike agree); programs without FP agree. The
coverage build is therefore used for coverage only; mismatches are gated on the plain bug-free build.
