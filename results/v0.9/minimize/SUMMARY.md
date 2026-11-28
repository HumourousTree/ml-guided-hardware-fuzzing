# Minimised detecting programs, Rocket (rocket/minimize.py)

Input: the first detections of the Rocket coverage-feedback campaign (run 46083050852, base profile, 5 seeds);
programs recovered by replaying the campaign stream (7 of 50 come after a mutation and are skipped). Test: replay
rule (same failure kind on the buggy core, bug-free core agrees with Spike). Local Docker, CI image, 2026-11-28.
Scripts: tools/minimize_scripts/.

43 minimised, all replay-confirmed: 43; 1-minimal: 43; body lines median 43 -> 1 (max 4); tests median 13.

| bug | seeds | body lines -> minimised | tests | failure | example minimised body |
|---|---|---|---|---|---|
| driver/144 | 4 | 44->1, 41->1, 45->3, 46->1 | 15, 15, 40, 22 | mismatch x5 | `mulhu x5, x15, x19` |
| driver/293 | 4 | 41->1, 44->1, 42->1, 43->4 | 11, 14, 12, 66 | mismatch x3 | `subw x3, x3, x30` |
| driver/347 | 5 | 42->2, 47->1, 43->1, 42->1, 47->1 | 22, 17, 12, 16, 13 | mismatch x7 | `sllw x7, x10, x29` |
| driver/361 | 5 | 41->0, 41->0, 44->0, 45->0, 45->0 | 9, 9, 9, 9, 9 | mismatch x1,x2,x3 | `(empty: prologue/epilogue alone)` |
| driver/673 | 2 | 46->2, 43->2 | 20, 29 | mismatch x11 | `divuw x11, x18, x1 ; sub x11, x0, x6` |
| driver/863 | 3 | 45->1, 43->1, 42->1 | 14, 13, 15 | mismatch x10 | `or x10, x10, x14` |
| driver/1094 | 5 | 48->1, 42->1, 41->1, 41->1, 44->1 | 20, 12, 13, 14, 16 | mismatch x14 | `srlw x14, x25, x4` |
| multiplexer/285 | 5 | 48->1, 43->1, 42->1, 45->1, 42->1 | 17, 13, 15, 12, 16 | mismatch fcsr | `fmul.d f7, f15, f2, dyn` |
| multiplexer/408 | 5 | 43->0, 45->0, 42->0, 43->0, 46->0 | 9, 9, 9, 9, 9 | nontermination | `(empty: prologue/epilogue alone)` |
| multiplexer/718 | 5 | 47->0, 44->0, 42->0, 43->0, 43->0 | 9, 8, 9, 9, 9 | nontermination | `(empty: prologue/epilogue alone)` |
