"""Each directed bug stimulus must (a) be a valid program, (b) pass on a clean DUT, and (c) detect a *software
mutant* that implements the intent of its bug.  This validates the stimulus design, not the injected RTL
(that needs Verilator: run `python3 -m harness.run_experiments --experiment E1 --suite all`)."""
import unittest
from sw_reference.golden_rv32i import RV32IReference, u32, sext
from harness import pico_campaign as pc
from harness.riscv_cosim import validate_program, compare_rvfi_trace
from tests.mock_dut import dut_trace


def _fields(insn): return insn & 0x7F, (insn >> 12) & 7, (insn >> 7) & 31, (insn >> 15) & 31


class Mut(RV32IReference):
    def _rewrite(self, f3_from, f3_to, opcodes):
        insn = self.peek_insn(); op, f3, _, _ = _fields(insn)
        if op in opcodes and f3 == f3_from:
            self._store_raw(self.pc, 4, (insn & ~0x7000) | (f3_to << 12))
            ev = super().step(); ev["insn"] = insn; self._store_raw(self.pc - 4 if False else ev["pc_rdata"], 4, insn); return ev
        return None


class AndToOr(Mut):
    def step(self):
        insn = self.peek_insn(); op, f3, rd, rs1 = _fields(insn)
        a = self.regs[rs1]; b = self.regs[(insn >> 20) & 31]; imm = u32(sext(insn >> 20, 12))
        ev = super().step()
        if op in (0x33, 0x13) and f3 == 7 and rd and not (op == 0x33 and (insn >> 25) == 1):
            self.regs[rd] = a | (b if op == 0x33 else imm); ev["rd_wdata"] = self.regs[rd]
        return ev


class Bit2Stuck(Mut):
    def step(self):
        insn = self.peek_insn(); op, f3, rd, rs1 = _fields(insn)
        if op in (0x03, 0x23) and rs1:
            imm = sext(insn >> 20, 12) if op == 0x03 else sext(((insn >> 25) << 5) | ((insn >> 7) & 31), 12)
            if (self.regs[rs1] + imm) & 4:
                save = self.regs[rs1]; self.regs[rs1] = u32(save - 4); ev = super().step(); self.regs[rs1] = save if rd != rs1 else self.regs[rs1]; return ev
        return super().step()


class SltUnsigned(Mut):
    def step(self):
        for a, b, ops in ((2, 3, (0x33, 0x13)), (4, 6, (0x63,)), (5, 7, (0x63,))):
            ev = self._rewrite(a, b, ops)
            if ev: return ev
        return super().step()


class BeqInverted(Mut):
    def step(self):
        return self._rewrite(0, 1, (0x63,)) or super().step()


class LuiAuipcX0(Mut):
    def step(self):
        insn = self.peek_insn(); op, _, rd, _ = _fields(insn); old = self.regs[rd]
        ev = super().step()
        if op in (0x37, 0x17) and rd:
            self.regs[rd] = old; ev["rd_addr"] = 0; ev["rd_wdata"] = 0
        return ev


class StoreAfterStore(Mut):
    prev = False
    def step(self):
        insn = self.peek_insn(); op = insn & 0x7F
        before = dict(self.mem)
        ev = super().step()
        if op == 0x23:
            if self.prev:
                self.mem = before; ev["mem_wmask"] = 0; ev["mem_wdata"] = 0
            self.prev = True
        elif op == 0x03:
            self.prev = False
        return ev


MUTANTS = {"and_to_or_alu": AndToOr, "mem_addr_bit2_stuck": Bit2Stuck, "slt_signed_as_unsigned": SltUnsigned,
           "beq_inverted": BeqInverted, "lui_auipc_rd_x0": LuiAuipcX0, "store_after_store_dropped": StoreAfterStore}


class TestStimuli(unittest.TestCase):
    def test_all_bug_stimuli_valid_and_clean_dut_passes(self):
        for b in list(MUTANTS) + ["stale_latched_branch", "approx_multiplier", "alu_operator_xor_or", "branch_inversion_blt",
                                  "writeback_forwarding_drop", "mem_handshake_drop"]:
            p = pc._generate_bug_targeted_stimulus(b); self.assertTrue(validate_program(p)["valid"], (b, validate_program(p)))
            words = [int(w, 0) for w in p["words"]]
            self.assertTrue(compare_rvfi_trace(p, dut_trace(words))["ok"], b)

    def test_each_stimulus_detects_its_software_mutant(self):
        for b, cls in MUTANTS.items():
            p = pc._generate_bug_targeted_stimulus(b); words = [int(w, 0) for w in p["words"]]
            res = compare_rvfi_trace(p, dut_trace(words, cls))
            self.assertTrue(res["mismatches"], f"{b}: stimulus does not expose the mutant")


if __name__ == "__main__":
    unittest.main()
