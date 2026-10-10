"""Software stand-in for the Verilated PicoRV32: an ISS wrapped to emit RVFI records with
PicoRV32's conventions (word-aligned mem_addr, rmask 0xF, replicated store data) and optional faults."""
import random
from sw_reference.golden_rv32i import RV32IReference, sext, u32
from harness.riscv_cosim import analyze_program, compare_rvfi_trace


class XoriOrDut(RV32IReference):
    """Fault: XORI with a negative immediate computes OR (cf. bug 'alu_operator_xor_or')."""
    def step(self):
        insn = self.peek_insn()
        pre_a = self.regs[(insn >> 15) & 31]
        ev = super().step()
        if (insn & 0x7F) == 0x13 and ((insn >> 12) & 7) == 4:
            imm = sext(insn >> 20, 12)
            rd = (insn >> 7) & 31
            if imm < 0 and rd:
                self.regs[rd] = u32(pre_a | u32(imm))
                ev["rd_wdata"] = self.regs[rd]
        return ev


def rtl_record(ev, order, cycles):
    rec = dict(ev)
    rec.update(valid=True, order=order, cycles=cycles)
    if ev["mem_rmask"] or ev["mem_wmask"]:
        rec["mem_addr"] = ev["mem_addr"] & ~3
    if ev["mem_wmask"]:
        size_bits = bin(ev["mem_wmask"]).count("1") * 8
        lane = (ev["mem_wdata"] >> (8 * ((ev["mem_wmask"] & -ev["mem_wmask"]).bit_length() - 1))) & ((1 << size_bits) - 1)
        rec["mem_wdata"] = sum(lane << (size_bits * i) for i in range(32 // size_bits)) & 0xFFFFFFFF
    return rec


def dut_trace(words, cls=RV32IReference, max_retire=None, base=0):
    dut = cls(words, base)
    end = base + 4 * len(words)
    trace = []
    n = 0
    while base <= dut.pc < end and (max_retire is None or n < max_retire):
        trace.append(rtl_record(dut.step(), n, 9 * (n + 1)))
        n += 1
    return trace


def program(words, pid="t"):
    return {"id": pid, "base": "0x00000000", "words": [f"0x{w:08x}" for w in words]}


def fails_on(cls):
    def fn(prog):
        words = [int(w, 0) for w in prog["words"]]
        if not analyze_program(words)["ok"]:
            return False
        return bool(compare_rvfi_trace(prog, dut_trace(words, cls))["mismatches"])
    return fn


# --- encoders -------------------------------------------------------------
def addi(rd, rs1, imm): return ((imm & 0xFFF) << 20) | (rs1 << 15) | (rd << 7) | 0x13
def xori(rd, rs1, imm): return ((imm & 0xFFF) << 20) | (rs1 << 15) | (4 << 12) | (rd << 7) | 0x13
def rtype(f7, rs2, rs1, f3, rd): return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | 0x33
def sw(rs2, rs1, imm): return (((imm >> 5) & 0x7F) << 25) | (rs2 << 20) | (rs1 << 15) | (2 << 12) | ((imm & 0x1F) << 7) | 0x23
def lw(rd, rs1, imm): return ((imm & 0xFFF) << 20) | (rs1 << 15) | (2 << 12) | (rd << 7) | 0x03
def lb(rd, rs1, imm): return ((imm & 0xFFF) << 20) | (rs1 << 15) | (rd << 7) | 0x03
def beq(rs1, rs2, off):
    return (((off >> 12) & 1) << 31) | (((off >> 5) & 0x3F) << 25) | (rs2 << 20) | (rs1 << 15) | (((off >> 1) & 0xF) << 8) | (((off >> 11) & 1) << 7) | 0x63
NOP = 0x13


def random_program(rng, n=24):
    w = []
    for r in range(1, 6):
        w.append(addi(r, 0, rng.choice([-1, 5, 15, 100, 0x7FF, -2048])))
    w.append(addi(10, 0, 0x300))
    while len(w) < n:
        k = rng.random()
        if k < .3: w.append(xori(rng.randrange(1, 9), rng.randrange(1, 9), rng.choice([-1, 7, -9, 3])))
        elif k < .5: w.append(rtype(0, rng.randrange(1, 9), rng.randrange(1, 9), rng.choice([0, 4, 6, 7]), rng.randrange(1, 9)))
        elif k < .6: w.append(sw(rng.randrange(1, 9), 10, 4 * rng.randrange(0, 8)))
        elif k < .7: w.append(lw(rng.randrange(1, 9), 10, 4 * rng.randrange(0, 8)))
        elif k < .8: w.append(lb(rng.randrange(1, 9), 10, rng.randrange(0, 32)))
        elif k < .9: w.append(beq(rng.randrange(1, 9), rng.randrange(1, 9), 8))
        else: w.append(addi(rng.randrange(1, 9), rng.randrange(1, 9), rng.choice([-5, 5, 2047])))
    w.append(NOP)
    return w
