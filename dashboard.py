"""Streamlit Dashboard for PicoRV32 Processor Co-Simulation,
Adaptive Delta-Debugging, and Dual-Layer Coverage Analytics."""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from harness.pico_campaign import reset_campaign_state

ROOT = Path(__file__).resolve().parent
TELEMETRY = ROOT / "logs" / "pico_campaign_telemetry.json"
TELEMETRY_ALT = ROOT / "logs" / "picorv32_telemetry.json"
STOP_FILE = ROOT / "logs" / "stop_campaign"
PROCESS_LOG = ROOT / "logs" / "dashboard_campaign.log"
RUN_STATE = ROOT / "logs" / "pico_campaign.pid"
HW = ROOT / "hw_design" / "picorv32_mutable.sv"
PROGRAMS = ROOT / "test_corpus" / "rv32i_programs.json"

st.set_page_config(
    page_title="PicoRV32 Fuzzing & Coverage Dashboard",
    page_icon="[P]",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("PicoRV32 Processor Co-Simulation & Coverage Analytics")
st.caption("Verilator dynamic C++ co-simulation with 2-stage memory handshakes, adaptive delta-debugging, and dual-layer coverage tracking.")


def read_last_n_bytes(path: Path, n: int = 4000) -> str:
    if not path.exists():
        return ""
    try:
        with open(path, 'rb') as f:
            try:
                f.seek(-n, os.SEEK_END)
            except OSError:
                f.seek(0)
            return f.read().decode('utf-8', errors='ignore')
    except Exception as e:
        return f"Error reading log: {e}"

def get_telemetry_data() -> dict | None:
    for path in (TELEMETRY, TELEMETRY_ALT):
        if path.exists():
            data = _read_json_retry(path)
            if data is not None:
                st.session_state.last_telemetry = data
                return data
    return st.session_state.get("last_telemetry")


def _read_json_retry(path: Path, attempts: int = 3, delay_seconds: float = 0.12) -> dict | None:
    for attempt in range(attempts):
        try:
            return json.loads(path.read_text())
        except Exception:
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
    return None


def clear_campaign_artifacts() -> None:
    STOP_FILE.unlink(missing_ok=True)
    PROCESS_LOG.unlink(missing_ok=True)
    reset_campaign_state(reset_programs=True)


if "campaign_process" not in st.session_state:
    st.session_state.campaign_process = None
if "last_telemetry" not in st.session_state:
    st.session_state.last_telemetry = None

process = st.session_state.campaign_process
worker_running = False

if process is not None and process.poll() is None:
    worker_running = True
elif RUN_STATE.exists():
    try:
        pid = int(RUN_STATE.read_text().strip())
        os.kill(pid, 0)
        worker_running = True
    except (ValueError, OSError):
        RUN_STATE.unlink(missing_ok=True)

if process is not None and process.poll() is not None:
    st.session_state.campaign_process = None
    process = None

# Sidebar Control Panel
with st.sidebar:
    st.header("Campaign Configuration")
    continuous = st.checkbox("Continuous Execution", value=False)
    duration = st.number_input("Time Budget (seconds)", min_value=5, max_value=1200, value=60, step=10, disabled=continuous)
    iterations = st.number_input("Maximum Iterations", min_value=1, max_value=500, value=3, step=1, disabled=continuous)

    fuzz_interval = st.slider(
        "Fuzzing Interval (N)",
        min_value=1,
        max_value=50,
        value=8,
        help="Interval N: Iteration K % N == 0 triggers Gemini expansion. Other iterations run local directed fuzzing.",
    )

    seed = st.number_input("RNG Seed", min_value=0, max_value=2147483647, value=20260825, step=1)

    st.markdown("---")
    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        launch = st.button("Start Campaign", type="primary", use_container_width=True, disabled=worker_running)
    with col_btn2:
        stop = st.button("Stop Campaign", use_container_width=True, disabled=not worker_running)

    col_btn3, col_btn4 = st.columns(2)
    with col_btn3:
        refresh = st.button("Refresh", use_container_width=True)
    with col_btn4:
        reset = st.button("Clear History", use_container_width=True, disabled=worker_running)

    # ── Bug Injection Panel ────────────────────────────────────────────────────
    st.markdown("---")
    st.header("Bug Injection Lab")
    _bug_injector_ok = False
    try:
        from hw_design.bug_injector import (
            list_bugs as _list_bugs,
            inject_multiple as _inject_multiple,
            restore_golden as _restore_golden,
            get_active_bugs as _get_active_bugs,
        )
        _bug_injector_ok = True
    except ImportError:
        st.caption("⚠️ bug_injector module not available.")

    if _bug_injector_ok:
        try:
            _all_bugs = _list_bugs()
            _active_ids = _get_active_bugs()

            # Active bug status badge
            if _active_ids:
                st.error(f"🐛 **Active Bugs:** {', '.join(_active_ids)}")
            else:
                st.success("✅ RTL in **PRISTINE GOLDEN** state (no active bugs)")

            # Bug selector
            _bug_options = list(_all_bugs.keys())
            _selected_bugs = st.multiselect(
                "Select Bug(s) to Inject",
                options=_bug_options,
                default=[],
                format_func=lambda b: f"{b} [{_all_bugs[b].get('level','?')}] — {_all_bugs[b].get('name','')}",
                help="Select one or more bugs from the taxonomy to inject into picorv32_mutable.sv",
                disabled=worker_running,
            )

            _inj_col, _rst_col = st.columns(2)
            with _inj_col:
                if st.button("Inject Selected", use_container_width=True,
                             disabled=worker_running or not _selected_bugs):
                    try:
                        ok = _inject_multiple(_selected_bugs)
                        if ok:
                            st.success(f"Injected: {', '.join(_selected_bugs)}")
                        else:
                            st.error("Injection failed — check marker positions in manifest.")
                    except Exception as exc:
                        st.error(f"Injection error: {exc}")
                    st.rerun()

            with _rst_col:
                if st.button("Restore Golden", use_container_width=True,
                             disabled=worker_running):
                    try:
                        _restore_golden()
                        st.success("RTL restored to pristine golden state.")
                    except Exception as exc:
                        st.error(f"Restore error: {exc}")
                    st.rerun()

            # Bug taxonomy table
            with st.expander("Bug Taxonomy", expanded=False):
                for bid, bdata in _all_bugs.items():
                    status_icon = "🔴" if bdata.get("active") else "⚪"
                    st.write(f"{status_icon} **{bid}** ({bdata.get('level','?')} — {bdata.get('subsystem','?')})")
                    st.caption(f"  {bdata.get('description','')}")
                    st.caption(f"  Symptom: {bdata.get('expected_symptom','')}")

        except Exception as exc:
            st.warning(f"Bug injector unavailable: {exc}")


if launch:
    STOP_FILE.unlink(missing_ok=True)
    PROCESS_LOG.unlink(missing_ok=True)
    log_handle = PROCESS_LOG.open("a")

    args = [
        sys.executable,
        "-m",
        "harness.pico_campaign",
        "--seed",
        str(int(seed)),
        "--fuzz-interval",
        str(int(fuzz_interval)),
    ]
    if continuous:
        args.append("--continuous")
    else:
        args.extend(["--seconds", str(int(duration)), "--iterations", str(int(iterations))])

    st.session_state.campaign_process = subprocess.Popen(
        args,
        cwd=ROOT,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    st.rerun()

if stop and worker_running:
    STOP_FILE.touch()
    st.info("Stop signal dispatched. Worker will exit following completion of current iteration.")

if refresh:
    st.rerun()

if reset:
    clear_campaign_artifacts()
    st.success("Campaign telemetry, temporary directories, and checkpoints cleared. Reset to default 3 seed programs.")
    st.rerun()

# Worker status notice
if worker_running:
    st.info("Worker status: Active. Simulation telemetry refreshing automatically.")
else:
    stale = _read_json_retry(TELEMETRY) if TELEMETRY.exists() else None
    if stale and stale.get("status") == "running":
        st.warning("Telemetry reports running state, but no active worker process was detected.")


def render_dashboard():
    telemetry = get_telemetry_data()
    if not telemetry:
        st.info("No active campaign telemetry. Configure parameters in the sidebar and select Start Campaign.")
        return

    iter_list = telemetry.get("iterations", [])
    latest_iter = iter_list[-1] if iter_list else {}

    # Primary Metric Cards
    status = str(telemetry.get("status", "unknown")).upper()
    total_iters = len(iter_list)
    overall_match = telemetry.get("overall_match_rate", 100.0)
    overall_cpi = telemetry.get("overall_avg_cpi", 0.0)
    llm_calls = telemetry.get("llm_call_count", 0)

    cov_info = telemetry.get("coverage", {})
    unified_cov = cov_info.get("unified_coverage_pct", 0.0)
    cumulative_func = cov_info.get("cumulative_functional_pct", cov_info.get("overall_functional_pct", 0.0))
    rtl_line_cov = cov_info.get("rtl_line_coverage_pct")
    rtl_toggle_cov = cov_info.get("rtl_toggle_coverage_pct")
    hy_seeds = cov_info.get("high_yield_seeds_count", 0)

    directed_fuzz = telemetry.get("directed_fuzzing", {})
    fuzz_yield = directed_fuzz.get("validity_yield_pct", 100.0)

    card1, card2, card3, card4, card5, card6, card7 = st.columns(7)
    card1.metric("Campaign Status", status)
    card2.metric("Completed Iterations", str(total_iters))
    card3.metric("Lockstep Match Rate", f"{overall_match}%")
    card4.metric("Average CPI", f"{overall_cpi:.2f}" if overall_cpi else "N/A")
    card5.metric("Unified Co-Coverage", f"{unified_cov}%")
    card6.metric("Cumulative Functional", f"{cumulative_func}%")
    card7.metric("Fuzzing Yield", f"{fuzz_yield}%")

    api_key_ok = telemetry.get("gemini_api_key_present", False)
    gemini_model = telemetry.get("gemini_model", "unknown")
    gemini_fallback = telemetry.get("gemini_fallback_model", "gemini-3.5-flash-lite")

    if api_key_ok:
        st.caption(f"Gemini API: Connected  |  Primary: {gemini_model}  |  Fallback: {gemini_fallback}")
    else:
        st.warning("Gemini API key not detected. Campaign running in local heuristic-only mode.")

    st.markdown("---")

    # Execution Mode and Diagnostics Row
    if latest_iter:
        current_mode = latest_iter.get("mode", "Idle")
        col_badge, col_diag1, col_diag2 = st.columns([1.5, 2, 2])

        with col_badge:
            st.subheader("Execution Mode")
            if "Delta-Debug" in current_mode or "Hardware Repair" in current_mode:
                st.error(f"{current_mode} (Adaptive Slicing & Repair)")
            elif "Expansion" in current_mode:
                st.success(f"{current_mode} (Target-Guided Synthesis)")
            else:
                st.info(f"{current_mode} (Loop A: Directed Heuristic Fuzzing)")

            st.caption(f"Campaign ID: {telemetry.get('campaign_id', 'N/A')}")
            st.caption(f"Target HDL: {Path(telemetry.get('hardware_target', 'hw_design/picorv32_mutable.sv')).name}")

        with col_diag1:
            st.subheader("Hardware Complexity (Yosys)")
            yosys = telemetry.get("yosys_stats", {})
            st.write(f"- Total Cell Count: **{yosys.get('total_cells', 'N/A')}** cells")
            st.write(f"- RTL Net/Wire Count: **{yosys.get('wires', 'N/A')}** wires")
            st.write(f"- Memory Blocks: **{yosys.get('memories', 'N/A')}** blocks")
            cov_tool = cov_info.get("verilator", {})
            st.write(
                f"- Verilator coverage artifacts: **{cov_tool.get('artifact_count', 0)}** "
                f"(tool: {'available' if cov_tool.get('tool_available') else 'unavailable'})"
            )

        with col_diag2:
            st.subheader("Latest Waveform Trace")
            vcd_path = latest_iter.get("vcd_path") or telemetry.get("latest_vcd")
            if vcd_path and os.path.exists(vcd_path):
                st.write(f"- File: `{Path(vcd_path).name}`")
                try:
                    with open(vcd_path, "rb") as f:
                        st.download_button(
                            label="Download VCD Waveform",
                            data=f.read(),
                            file_name=Path(vcd_path).name,
                            mime="application/octet-stream",
                        )
                except Exception:
                    st.write(f"Trace path: `{vcd_path}`")
            else:
                st.write("- Waveform file not available.")

    # ── Section: Dual-Layer Coverage Analytics (Graphical Visualizations) ─────
    st.markdown("---")
    st.subheader("Verification Co-Coverage Analytics")
    cov_m1, cov_m2, cov_m3, cov_m4 = st.columns(4)
    cov_m1.metric("Unified Co-Coverage (C_Unified)", f"{unified_cov}%")
    cov_m2.metric("Cumulative Functional (Union)", f"{cumulative_func}%")
    cov_m3.metric("Instruction Diversity", f"{cov_info.get('instruction_diversity_pct', 0.0)}%")
    cov_m4.metric("Register Pattern Coverage", f"{cov_info.get('register_pattern_pct', 0.0)}%")
    st.caption(
        "RTL line coverage: "
        f"{rtl_line_cov if rtl_line_cov is not None else 'N/A'}%; "
        "toggle coverage: "
        f"{rtl_toggle_cov if rtl_toggle_cov is not None else 'N/A'}%. "
        f"aggregate Verilator coverage: {cov_info.get('verilator', {}).get('total_coverage_pct', 'N/A')}%. "
        "N/A means Verilator produced no parseable percentage, not zero coverage."
    )

    st.caption(f"High-Yield Seed Bank: {hy_seeds} optimal test programs preserved in test_corpus/high_yield/")

    chart_cov1, chart_cov2 = st.columns(2)

    insn_dist = cov_info.get("insn_type_distribution", {})
    with chart_cov1:
        st.write("**Instruction Type Distribution:**")
        if insn_dist:
            st.bar_chart(insn_dist)
        else:
            st.caption("No instruction distribution data recorded yet.")

    pat_dist = cov_info.get("register_pattern_distribution", {})
    with chart_cov2:
        st.write("**Architectural Register ($x0-$x31) Operand Patterns:**")
        if pat_dist:
            st.bar_chart(pat_dist)
        else:
            st.caption("No register pattern distribution data recorded yet.")

    # Cumulative Coverage Growth Line Chart
    cov_history = cov_info.get("coverage_history", [])
    if len(cov_history) > 1:
        st.write("**Cumulative Coverage Progression Across Iterations (Monotonic Progression):**")
        cov_chart_data = {
            "Iteration": [h.get("iteration") for h in cov_history],
            "Unified Co-Coverage (%)": [h.get("unified_coverage_pct", h.get("rtl_coverage_pct", 0.0)) for h in cov_history],
            "Cumulative Functional (%)": [h.get("functional_coverage_pct", 0.0) for h in cov_history],
            "RTL Line Coverage (%)": [h.get("rtl_line_coverage_pct", h.get("rtl_coverage_pct", 0.0)) for h in cov_history],
        }
        st.line_chart(cov_chart_data, x="Iteration", color=["#636EFA", "#00CC96", "#AB63FA"])

    # ── Section: Directed Fuzzing & Testbench Quality ──────────────────────────
    st.markdown("---")
    st.subheader("Directed Fuzzing & Testbench Quality")
    fuzz_col1, fuzz_col2, fuzz_col3 = st.columns(3)
    tot_gen = directed_fuzz.get("total_generated", 0)
    v_cnt = directed_fuzz.get("valid_count", 0)
    inv_cnt = directed_fuzz.get("invalid_count", 0)

    fuzz_col1.metric("Total Candidates Generated", str(tot_gen))
    fuzz_col2.metric("Valid Candidates Simulated", str(v_cnt))
    fuzz_col3.metric("Invalid Candidates Filtered", str(inv_cnt), help="Discarded before simulation to prevent invalid opcode / unaligned memory traps")

    val_history = directed_fuzz.get("validation_history", [])
    if val_history:
        fuzz_table = []
        for v in reversed(val_history[-20:]):
            fuzz_table.append({
                "Iteration": v.get("iteration"),
                "Program ID": v.get("program_id"),
                "Type": v.get("mutation_type"),
                "Status": "VALID (Simulated)" if v.get("valid") else "INVALID (Filtered Out)",
                "Details / Filter Reason": v.get("reason"),
                "Instructions": v.get("instruction_count"),
            })
        st.dataframe(fuzz_table, use_container_width=True, hide_index=True)

    # ── Section: Adaptive Delta-Debugging & Root-Cause Pinpointing ─────────────
    delta_debug = telemetry.get("delta_debug", {})
    first_mismatch = latest_iter.get("first_mismatch") if latest_iter else None

    if first_mismatch or delta_debug.get("active"):
        st.markdown("---")
        st.subheader("Adaptive Delta-Debugging Protocol & Sliced Reproducer")
        
        m_col1, m_col2, m_col3 = st.columns(3)
        fail_k = delta_debug.get("failure_index_k", first_mismatch.get("retire_order") if first_mismatch else "N/A")
        m_col1.metric("First Failure Index K", str(fail_k))
        
        failing_disasm = delta_debug.get("failing_instruction_disasm") or (first_mismatch or {}).get("insn_disasm", "Unknown")
        failing_hex = delta_debug.get("failing_instruction_hex") or (first_mismatch or {}).get("insn_hex", "")
        m_col2.metric("Failing Instruction", f"{failing_disasm} ({failing_hex})")
        
        reproducer_len = delta_debug.get("minimal_program_length", "N/A")
        m_col3.metric("Sliced Reproducer Length", f"{reproducer_len} instructions")

        if first_mismatch:
            diff_col1, diff_col2 = st.columns(2)
            with diff_col1:
                st.write("**Software ISS Expected State (Authoritative):**")
                st.json(first_mismatch.get("expected", {}))
            with diff_col2:
                st.write("**Hardware Actual State (PicoRV32 RVFI Output):**")
                st.json(first_mismatch.get("actual", {}))
            if "differences" in first_mismatch:
                st.write("**Exact Divergent Fields:**")
                st.json(first_mismatch.get("differences"))

        shrink_phases = delta_debug.get("shrink_phases", [])
        grow_phases = delta_debug.get("grow_phases", [])
        if shrink_phases or grow_phases:
            phase_col1, phase_col2 = st.columns(2)
            with phase_col1:
                st.write("**Vector Shrink Progression:**")
                st.json(shrink_phases)
            with phase_col2:
                st.write("**Regression Grow Verification Schedule:**")
                st.json(grow_phases)

    # ── Section: Performance Trends ───────────────────────────────────────────
    if len(iter_list) > 1:
        st.markdown("---")
        st.subheader("Campaign Performance Trends")
        chart_col1, chart_col2 = st.columns(2)

        chart_data = {
            "Iteration": [it.get("iteration") for it in iter_list],
            "Match Rate (%)": [it.get("match_rate_pct", 100.0) for it in iter_list],
            "Cycles Per Instruction (CPI)": [it.get("avg_cpi", 0.0) for it in iter_list],
            "Retired Instructions": [it.get("retired_total", 0) for it in iter_list],
        }

        with chart_col1:
            st.line_chart(
                data=chart_data,
                x="Iteration",
                y="Match Rate (%)",
                color="#00CC96",
            )

        with chart_col2:
            st.line_chart(
                data=chart_data,
                x="Iteration",
                y="Cycles Per Instruction (CPI)",
                color="#636EFA",
            )

    # ── Section: Iteration History Table ──────────────────────────────────────
    st.markdown("---")
    st.subheader("Iteration History")
    rows = []
    for it in reversed(iter_list):
        dd_info = it.get("delta_debug_info") or {}
        dd_status = dd_info.get("status", "N/A") if dd_info else "N/A"
        _active_bugs_iter = it.get("active_bugs", [])
        rows.append({
            "Iteration": it.get("iteration"),
            "Mode": it.get("mode"),
            "Lockstep Status": "Pass" if it.get("comparison_ok") else "Divergence",
            "Match Rate": f"{it.get('match_rate_pct', 100.0)}%",
            "CPI": f"{it.get('avg_cpi', 0.0):.2f}",
            "Unified Coverage": f"{it.get('unified_coverage_pct', 0.0)}%",
            "Functional Coverage": f"{it.get('functional_coverage_pct', 0.0)}%",
            "Retired": it.get("retired_total"),
            "Valid Tests": it.get("valid_testcases", it.get("program_count", 0)),
            "Invalid Filtered": it.get("invalid_testcases", 0),
            "Mismatches": it.get("mismatch_total"),
            "Active Bugs": ", ".join(_active_bugs_iter) if _active_bugs_iter else "None",
            "Bug Programs": it.get("bug_targeted_programs", 0),
            "LLM Invoked": "Yes" if it.get("llm_called") else "No",
            "LLM Source": it.get("llm_source") or "None",
            "RTL Improved": "Yes" if it.get("rtl_improved") else "No",
            "Delta-Debug Status": dd_status,
            "RTL SHA256": str(it.get("rtl_sha256", ""))[:10],
            "Timestamp": it.get("finished_at", "")[-12:-4],
        })
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)

    # ── Section: Verilator & Process Diagnostics Log ──────────────────────────
    with st.expander("Verilator Compiler & Worker Process Diagnostics", expanded=False):
        if latest_iter.get("lint_log"):
            st.write("**Verilator Lint Output:**")
            st.code(latest_iter["lint_log"][:3000])
        if PROCESS_LOG.exists():
            st.write("**Worker Console Output:**")
            st.code(read_last_n_bytes(PROCESS_LOG))


if hasattr(st, "fragment"):
    @st.fragment(run_every="2s")
    def render_dashboard_fragment() -> None:
        render_dashboard()

    render_dashboard_fragment()
else:
    render_dashboard()
