"""Immutable, dependency-free RV32IM architectural reference for lockstep checks.

The model is *architectural*: it reports the effective (possibly unaligned)
memory address of a load/store.  Any RVFI presentation convention of the DUT
(e.g. PicoRV32 reports a word-aligned ``mem_addr`` and expresses the byte lane
through ``mem_rmask``/``mem_wmask``) is applied by the scoreboard in
``harness.riscv_cosim`` and is documented there, so that this file never has to
change when the DUT convention is clarified.
"""

MASK32 = 0xFFFF_FFFF


def u32(value: int) -> int:
    return value & MASK32


def s32(value: int) -> int:
    value &= MASK32
    return value - (1 << 32) if value & (1 << 31) else value


def sext(value: int, width: int) -> int:
    value &= (1 << width) - 1
    return value - (1 << width) if value & (1 << (width - 1)) else value


def _div_trunc(a: int, b: int) -> int:
    """Signed integer division rounding toward zero (exact, no floating point)."""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


class RV32IReference:
    """Pure Python RV32IM ISA golden reference model."""

    def __init__(self, words: list[int], base: int = 0):
        self.pc = u32(base)
        self.regs = [0] * 32
        self.mem: dict[int, int] = {}
        for index, word in enumerate(words):
            self._store_raw(base + index * 4, 4, word)

    def clone(self) -> "RV32IReference":
        """Independent copy of the architectural state (used by the delta debugger)."""
        other = RV32IReference.__new__(RV32IReference)
        other.pc = self.pc
        other.regs = list(self.regs)
        other.mem = dict(self.mem)
        return other

    def _load_raw(self, address: int, size: int) -> int:
        return sum(self.mem.get(u32(address + offset), 0) << (8 * offset) for offset in range(size))

    def _store_raw(self, address: int, size: int, value: int) -> None:
        for offset in range(size):
            self.mem[u32(address + offset)] = (value >> (8 * offset)) & 0xFF

    def peek_insn(self) -> int:
        return self._load_raw(self.pc, 4)

    def step(self) -> dict:
        pc = self.pc
        insn = self._load_raw(pc, 4)
        opcode = insn & 0x7F
        rd = (insn >> 7) & 31
        funct3 = (insn >> 12) & 7
        rs1 = (insn >> 15) & 31
        rs2 = (insn >> 20) & 31
        funct7 = (insn >> 25) & 0x7F

        a = self.regs[rs1]
        b = self.regs[rs2]
        next_pc = u32(pc + 4)
        value = None
        mem_addr = mem_rmask = mem_wmask = mem_rdata = mem_wdata = 0

        i_imm = sext(insn >> 20, 12)
        s_imm = sext(((insn >> 25) << 5) | rd, 12)
        b_imm = sext(((insn >> 31) << 12) | (((insn >> 7) & 1) << 11) | (((insn >> 25) & 0x3F) << 5) | (((insn >> 8) & 0xF) << 1), 13)
        u_imm = insn & 0xFFFFF000
        j_imm = sext(((insn >> 31) << 20) | (((insn >> 12) & 0xFF) << 12) | (((insn >> 20) & 1) << 11) | (((insn >> 21) & 0x3FF) << 1), 21)

        if opcode == 0x37:  # LUI
            value = u_imm
        elif opcode == 0x17:  # AUIPC
            value = u32(pc + u_imm)
        elif opcode == 0x6F:  # JAL
            value = u32(pc + 4)
            next_pc = u32(pc + j_imm)
        elif opcode == 0x67 and funct3 == 0:  # JALR
            value = u32(pc + 4)
            next_pc = u32((a + i_imm) & ~1)
        elif opcode == 0x63:  # BRANCH
            taken = ((funct3 == 0 and a == b) or
                     (funct3 == 1 and a != b) or
                     (funct3 == 4 and s32(a) < s32(b)) or
                     (funct3 == 5 and s32(a) >= s32(b)) or
                     (funct3 == 6 and a < b) or
                     (funct3 == 7 and a >= b))
            if taken:
                next_pc = u32(pc + b_imm)
        elif opcode == 0x13:  # OP-IMM
            shamt = (insn >> 20) & 31
            if funct3 == 0:    value = u32(a + i_imm)
            elif funct3 == 2:  value = int(s32(a) < i_imm)
            elif funct3 == 3:  value = int(a < u32(i_imm))
            elif funct3 == 4:  value = a ^ u32(i_imm)
            elif funct3 == 6:  value = a | u32(i_imm)
            elif funct3 == 7:  value = a & u32(i_imm)
            elif funct3 == 1:  value = u32(a << shamt)
            elif funct3 == 5:  value = u32(s32(a) >> shamt) if funct7 == 0x20 else a >> shamt
        elif opcode == 0x33:  # OP / RV32M
            shamt = b & 31
            if funct7 == 0x01:  # RV32M standard extension
                if funct3 == 0:    # MUL
                    value = u32(a * b)
                elif funct3 == 1:  # MULH (signed x signed)
                    value = u32((s32(a) * s32(b)) >> 32)
                elif funct3 == 2:  # MULHSU (signed x unsigned)
                    value = u32((s32(a) * u32(b)) >> 32)
                elif funct3 == 3:  # MULHU (unsigned x unsigned)
                    value = u32((u32(a) * u32(b)) >> 32)
                elif funct3 == 4:  # DIV (signed / signed)
                    sa, sb = s32(a), s32(b)
                    if sb == 0: value = 0xFFFFFFFF
                    elif sa == -0x80000000 and sb == -1: value = 0x80000000
                    else: value = u32(_div_trunc(sa, sb))
                elif funct3 == 5:  # DIVU (unsigned / unsigned)
                    value = 0xFFFFFFFF if b == 0 else u32(a // b)
                elif funct3 == 6:  # REM (signed remainder)
                    sa, sb = s32(a), s32(b)
                    if sb == 0: value = u32(sa)
                    elif sa == -0x80000000 and sb == -1: value = 0
                    else: value = u32(sa - _div_trunc(sa, sb) * sb)
                elif funct3 == 7:  # REMU (unsigned remainder)
                    value = u32(a) if b == 0 else u32(a % b)
            else:
                if funct3 == 0:    value = u32(a - b) if funct7 == 0x20 else u32(a + b)
                elif funct3 == 1:  value = u32(a << shamt)
                elif funct3 == 2:  value = int(s32(a) < s32(b))
                elif funct3 == 3:  value = int(a < b)
                elif funct3 == 4:  value = a ^ b
                elif funct3 == 5:  value = u32(s32(a) >> shamt) if funct7 == 0x20 else a >> shamt
                elif funct3 == 6:  value = a | b
                elif funct3 == 7:  value = a & b
        elif opcode == 0x03:  # LOAD
            sizes = {0: 1, 1: 2, 2: 4, 4: 1, 5: 2}
            if funct3 not in sizes:
                raise ValueError(f"unsupported load instruction: 0x{insn:08x} at 0x{pc:08x}")
            size = sizes[funct3]
            mem_addr = u32(a + i_imm)
            raw = self._load_raw(mem_addr, size)
            aligned_addr = mem_addr & ~3
            mem_rdata = self._load_raw(aligned_addr, 4)
            mem_rmask = 0xF
            value = raw if funct3 in (4, 5) else u32(sext(raw, size * 8))
        elif opcode == 0x23:  # STORE
            sizes = {0: 1, 1: 2, 2: 4}
            if funct3 not in sizes:
                raise ValueError(f"unsupported store instruction: 0x{insn:08x} at 0x{pc:08x}")
            size = sizes[funct3]
            mem_addr = u32(a + s_imm)
            self._store_raw(mem_addr, size, b)
            byte_offset = mem_addr & 3
            mem_wmask = ((1 << size) - 1) << byte_offset
            mem_wdata = (b & ((1 << (size * 8)) - 1)) << (byte_offset * 8)
        else:
            raise ValueError(f"unsupported RV32I instruction: 0x{insn:08x} at 0x{pc:08x}")

        if value is not None and rd != 0:
            self.regs[rd] = u32(value)
        self.regs[0] = 0
        self.pc = next_pc

        return {
            "pc_rdata": pc,
            "pc_wdata": self.pc,
            "insn": insn,
            "rd_addr": rd if (value is not None and rd != 0) else 0,
            "rd_wdata": self.regs[rd] if (value is not None and rd != 0) else 0,
            "mem_addr": mem_addr,
            "mem_rmask": mem_rmask,
            "mem_wmask": mem_wmask,
            "mem_rdata": mem_rdata,
            "mem_wdata": mem_wdata,
        }


# ── Non-intrusive Functional Coverage Tracker ────────────────────────────────

class CoverageTracker:
    """Independent coverage helper that samples execution traces without mutating the ISS."""

    INSN_TYPES = ["ALU_REG", "MUL_DIV", "ALU_IMM", "BRANCH", "JUMP", "LOAD", "STORE", "LUI_AUIPC"]
    REGISTER_PATTERNS = ["all_zeros", "all_ones", "walking_ones", "walking_zeros", "alternating_bits", "boundary_extremes", "arbitrary_data"]

    def __init__(self):
        self.insn_type_counts = {t: 0 for t in self.INSN_TYPES}
        self.pattern_counts = {p: 0 for p in self.REGISTER_PATTERNS}
        self.total_instructions = 0

    def sample_instruction(self, insn: int, rd_val: int = 0, wrote_rd: bool = True) -> None:
        """Record one retired instruction.

        ``wrote_rd`` must be False for instructions that write no register
        (stores, branches, writes to x0); their ``rd_val`` of 0 is *not* an
        observed all-zeros operand pattern and would otherwise inflate it.
        """
        opcode = insn & 0x7F
        funct7 = (insn >> 25) & 0x7F
        if opcode == 0x33:
            if funct7 == 0x01:
                self.insn_type_counts["MUL_DIV"] += 1
            else:
                self.insn_type_counts["ALU_REG"] += 1
        elif opcode == 0x13: self.insn_type_counts["ALU_IMM"] += 1
        elif opcode == 0x63: self.insn_type_counts["BRANCH"] += 1
        elif opcode in (0x6F, 0x67): self.insn_type_counts["JUMP"] += 1
        elif opcode == 0x03: self.insn_type_counts["LOAD"] += 1
        elif opcode == 0x23: self.insn_type_counts["STORE"] += 1
        elif opcode in (0x37, 0x17): self.insn_type_counts["LUI_AUIPC"] += 1
        self.total_instructions += 1

        if not wrote_rd:
            return
        val = u32(rd_val)
        if val == 0: self.pattern_counts["all_zeros"] += 1
        elif val == 0xFFFFFFFF: self.pattern_counts["all_ones"] += 1
        elif val in (0x55555555, 0xAAAAAAAA): self.pattern_counts["alternating_bits"] += 1
        elif val in (0x7FFFFFFF, 0x80000000, 1, 0xFFFFFFFE): self.pattern_counts["boundary_extremes"] += 1
        elif bin(val).count("1") == 1: self.pattern_counts["walking_ones"] += 1
        elif bin(val).count("0") == 1: self.pattern_counts["walking_zeros"] += 1
        else: self.pattern_counts["arbitrary_data"] += 1

    def get_summary(self) -> dict:
        covered_types = sum(1 for c in self.insn_type_counts.values() if c > 0)
        insn_pct = round((covered_types / max(1, len(self.INSN_TYPES))) * 100.0, 1)
        covered_pats = sum(1 for c in self.pattern_counts.values() if c > 0)
        pat_pct = round((covered_pats / max(1, len(self.REGISTER_PATTERNS))) * 100.0, 1)
        return {
            "overall_functional_pct": round((insn_pct + pat_pct) / 2.0, 1),
            "instruction_diversity_pct": insn_pct,
            "register_pattern_pct": pat_pct,
            "insn_type_distribution": dict(self.insn_type_counts),
            "register_pattern_distribution": dict(self.pattern_counts),
        }
