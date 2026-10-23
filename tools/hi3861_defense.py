#!/usr/bin/env python3
"""Run the CP4 Hi3861L LAN-wake defense routine offline.

The receiver at UDP 50161 calls a defense/statistics routine at runtime address
0x420a82 before asserting GPIO 2. This tool decodes the actual RISC-V
instructions in that routine and executes the narrow subset they use. It also
shows the receiver gate around 0x420dc4, where the documented two-byte patch
replaces a conditional branch with an unconditional jump.

This is deliberately not a full MCU emulator:

- ROM logging is treated as having no relevant global writes.
- The time function is replaced by the supplied value.
- ROM save/restore prologues are skipped.
- The project's inferred vendor `li48` instruction is decoded separately.

The resulting model explains the counter thresholds in
docs/HI3861-LAN-PATCH.md. It is not physical measurement, and it does not model
tick wrap, signed overflow, deep-sleep time advancement, packet buffering,
GPIO wiring, or the radio.

Inputs are a CP4 Hi3861L OTA (the tool expands it) or a kernel.bin written by
`hi3861_ota.py unpack`. The kernel must match the documented 2.139 source or
patched image, because the addresses are specific to that build.

Requires capstone: `pip install capstone`.

Examples:
    python tools/hi3861_defense.py app-root/Hi3861L_ezviz_sdk_ota.bin
    python tools/hi3861_defense.py --kernel hi3861-unpacked/kernel.bin --json
    python tools/hi3861_defense.py --kernel hi3861-unpacked/kernel.bin --listing
"""

from __future__ import annotations

import argparse
import json
import random
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from capstone import CS_ARCH_RISCV, CS_MODE_RISCVC, CS_MODE_RISCV32, Cs
except ImportError:
    print("error: capstone is required; install it with "
          "'pip install capstone'", file=sys.stderr)
    raise SystemExit(2)

try:
    from hi3861_ota import (
        Hi3861Error,
        PATCHED_EXPANDED_SHA256,
        SOURCE_EXPANDED_SHA256,
        Ota,
        sha256,
    )
except ImportError as error:
    print(f"error: cannot import hi3861_ota.py: {error}", file=sys.stderr)
    raise SystemExit(2)

IMAGE_BASE = 0x40D000
DEFENSE_START = 0x420A82
DEFENSE_END = 0x420BE0
DEFENSE_RETURN = 0x420AF0
TIME_FUNCTION = 0x4350FC
LOG_FUNCTION = 0x3FA4AC
MAX_STEPS = 10000

GATE_START = 0x420DB4
GATE_END = 0x420DF2
GATE_BRANCH = 0x420DC4
GATE_ORIGINAL = bytes.fromhex("09e5")
GATE_PATCHED = bytes.fromhex("29a0")

STATE_FIELDS = {
    "anchor": 0xE227C,
    "previous": 0xE2278,
    "current": 0xE21D0,
    "day": 0xE21D4,
    "alarm": 0xE21CC,
    "alarms": 0xE21D8,
}
REGISTERS = (
    "zero ra sp gp tp t0 t1 t2 s0 s1 a0 a1 a2 a3 a4 a5 a6 a7 "
    "s2 s3 s4 s5 s6 s7 s8 s9 s10 s11 t3 t4 t5 t6"
).split()

KNOWN_KERNELS = {
    SOURCE_EXPANDED_SHA256: "source 2.139",
    PATCHED_EXPANDED_SHA256: "patched 2.139",
}


@dataclass(frozen=True)
class Instruction:
    address: int
    size: int
    mnemonic: str
    operands: list[str]
    annotation: str = ""

    @property
    def text(self) -> str:
        if not self.operands:
            return self.mnemonic
        return f"{self.mnemonic} {', '.join(self.operands)}"


def _signed(value: int) -> int:
    return value if value < 0x80000000 else value - 0x100000000


def _gate_state(image: bytes) -> tuple[str, str]:
    offset = GATE_BRANCH - IMAGE_BASE
    value = image[offset:offset + 2]
    if value == GATE_ORIGINAL:
        return "original", value.hex()
    if value == GATE_PATCHED:
        return "patched", value.hex()
    return "unexpected", value.hex()


def _printable_string(image: bytes, address: int) -> str:
    offset = address - IMAGE_BASE
    if not 0 <= offset < len(image):
        return ""
    raw = image[offset:offset + 160].split(b"\0", 1)[0]
    if raw and all(32 <= byte < 127 or byte in (9, 10, 13) for byte in raw):
        return repr(raw.decode("ascii"))
    return ""


class Decoder:
    def __init__(self, image: bytes):
        self.image = image
        self.capstone = Cs(
            CS_ARCH_RISCV, CS_MODE_RISCV32 | CS_MODE_RISCVC)

    def one(self, address: int) -> Instruction:
        offset = address - IMAGE_BASE
        if not 0 <= offset + 2 <= len(self.image):
            raise Hi3861Error(f"address 0x{address:x} is outside the kernel")
        halfword = struct.unpack_from("<H", self.image, offset)[0]
        if halfword & 0xF07F == 0x1F:
            if offset + 6 > len(self.image):
                raise Hi3861Error(
                    f"truncated li48 instruction at 0x{address:x}")
            value = struct.unpack_from("<I", self.image, offset + 2)[0]
            register = REGISTERS[(halfword >> 7) & 31]
            annotation = ""
            if IMAGE_BASE <= value < IMAGE_BASE + len(self.image):
                annotation = _printable_string(self.image, value)
            return Instruction(address, 6, "li48", [register, hex(value)],
                               annotation)
        size = 2 if halfword & 3 != 3 else 4
        decoded = next(
            self.capstone.disasm(self.image[offset:offset + size], address,
                                 count=1),
            None,
        )
        if decoded is None:
            raise Hi3861Error(
                f"cannot decode instruction 0x{halfword:04x} at 0x{address:x}")
        operands = decoded.op_str.split(", ") if decoded.op_str else []
        annotation = ""
        if decoded.mnemonic in ("jal", "j", "c.j", "c.jal",
                               "bgeu", "bge", "blt", "beq", "bne",
                               "c.beqz", "c.bnez"):
            try:
                target = address + int(operands[-1], 0)
                annotation = f"-> 0x{target:x}"
            except (ValueError, IndexError):
                pass
        return Instruction(address, size, decoded.mnemonic, operands,
                           annotation)

    def range(self, start: int, end: int) -> dict[int, Instruction]:
        code = {}
        address = start
        while address < end:
            instruction = self.one(address)
            code[address] = instruction
            address += instruction.size
        if address != end:
            raise Hi3861Error(
                f"instruction at 0x{address - 4:x} runs past 0x{end:x}")
        return code


def print_listing(decoder: Decoder) -> None:
    for title, start, end in (
        ("defense", DEFENSE_START, DEFENSE_END),
        ("receiver gate", GATE_START, GATE_END),
    ):
        print(f"{title} 0x{start:x}..0x{end:x}")
        for instruction in decoder.range(start, end).values():
            print(f"{instruction.address:08x}  {instruction.mnemonic:12s} "
                  f"{' '.join(instruction.operands):40s} {instruction.annotation}")
        print()


class Defense:
    def __init__(self, code: dict[int, Instruction]):
        self.code = code
        self.memory = {address: 0 for address in STATE_FIELDS.values()}

    def state(self) -> dict[str, int]:
        return {name: self.memory[address]
                for name, address in STATE_FIELDS.items()}

    def set_state(self, **state: int) -> None:
        for name, value in state.items():
            if name not in STATE_FIELDS:
                raise ValueError(f"unknown defense state {name!r}")
            self.memory[STATE_FIELDS[name]] = value

    def call(self, now: int) -> int:
        registers = {name: 0 for name in REGISTERS}
        pc = DEFENSE_START + 4  # skip the ROM save prologue at 0x420a82

        def value(operand: str) -> int:
            return registers[operand] if operand in registers else int(operand, 0)

        def address(operand: str) -> int:
            offset, register = operand.rstrip(")").split("(")
            return (value(offset) + value(register)) & 0xFFFFFFFF

        for _ in range(MAX_STEPS):
            if pc == DEFENSE_RETURN:
                return registers["a0"]
            if pc not in self.code:
                raise Hi3861Error(f"execution left the modeled routine at 0x{pc:x}")
            instruction = self.code[pc]
            mnemonic = instruction.mnemonic
            operands = instruction.operands
            next_pc = pc + instruction.size

            if mnemonic in ("li48", "c.li", "c.mv"):
                registers[operands[0]] = value(operands[1])
            elif mnemonic in ("c.lw", "lw"):
                registers[operands[0]] = self.memory.get(address(operands[1]), 0)
            elif mnemonic in ("c.sw", "sw"):
                self.memory[address(operands[1])] = value(operands[0])
            elif mnemonic == "c.addi":
                registers[operands[0]] += value(operands[1])
            elif mnemonic == "c.add":
                registers[operands[0]] += value(operands[1])
            elif mnemonic == "sub":
                registers[operands[0]] = value(operands[1]) - value(operands[2])
            elif mnemonic == "addi":
                registers[operands[0]] = value(operands[1]) + value(operands[2])
            elif mnemonic == "div":
                left, right = _signed(value(operands[1])), _signed(value(operands[2]))
                quotient = abs(left) // abs(right)
                registers[operands[0]] = (
                    quotient if (left < 0) == (right < 0) else -quotient)
            elif mnemonic == "slti":
                registers[operands[0]] = int(
                    _signed(value(operands[1])) < value(operands[2]))
            elif mnemonic in ("bgeu", "bge", "blt", "beq", "bne"):
                left, right = value(operands[0]), value(operands[1])
                taken = {
                    "bgeu": left >= right,
                    "bge": _signed(left) >= _signed(right),
                    "blt": _signed(left) < _signed(right),
                    "beq": left == right,
                    "bne": left != right,
                }[mnemonic]
                if taken:
                    next_pc = pc + value(operands[2])
            elif mnemonic in ("c.bnez", "c.beqz"):
                if (value(operands[0]) != 0) == (mnemonic == "c.bnez"):
                    next_pc = pc + value(operands[1])
            elif mnemonic in ("c.j", "j"):
                next_pc = pc + value(operands[-1])
            elif mnemonic in ("jal", "c.jal"):
                target = pc + value(operands[-1])
                if target == TIME_FUNCTION:
                    registers["a0"] = now
                elif target == LOG_FUNCTION:
                    pass
                else:
                    raise Hi3861Error(
                        f"unexpected call target 0x{target:x} at 0x{pc:x}")
            else:
                raise Hi3861Error(
                    f"unsupported instruction at 0x{pc:x}: {instruction.text}")

            for name, register_value in registers.items():
                registers[name] = register_value & 0xFFFFFFFF
            registers["zero"] = 0
            pc = next_pc
        raise Hi3861Error("instruction limit reached in the defense routine")


def model(state: dict[str, int], now: int) -> tuple[int, dict[str, int]]:
    """Independent expression of the decoded routine, for cross-checking."""
    state = state.copy()
    if state["anchor"] == 0:
        state["anchor"] = now
    day = (now - state["anchor"]) // 86400
    if day > 2:
        state.update(previous=0, current=1, anchor=now, alarm=0, day=0)
        return 0, state
    changed = day != state["day"]
    if changed:
        state["previous"] = state["current"]
        if day == 2 and state["day"] == 0:
            state["previous"] = 0
        state.update(current=1, day=day, alarm=0)
    else:
        state["current"] += 1
    if day == 2:
        while state["anchor"] < now - 86400 + 1:
            state["anchor"] += 86400
        state["day"] = 0
    elif changed:
        return int(state["previous"] < 200), state
    result = int(state["previous"] < 200)
    if state["current"] >= 100:
        result = 0
        if state["alarm"] != 1:
            state["alarm"] = 1
            state["alarms"] += 1
    return result, state


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise Hi3861Error(message)


def run_scenarios(code: dict[int, Instruction]) -> dict:
    results: dict[str, dict] = {}

    for count in (99, 100, 101, 200, 240):
        defense = Defense(code)
        returns = [defense.call(1000) for _ in range(count)]
        _expect(sum(returns) == min(count, 99),
                f"{count}-packet burst accepted {sum(returns)} packets")
        results[f"{count}_packets"] = {
            "accepted": sum(returns),
            "last_return": returns[-1],
            "state": defense.state(),
        }

    defense = Defense(code)
    for _ in range(240):
        defense.call(1000)
    next_return = defense.call(87400)
    _expect(next_return == 0, "the next bucket was not blocked")
    results["next_bucket_after_240"] = {
        "return": next_return, "state": defense.state()}
    second_return = defense.call(173800)
    _expect(second_return == 1, "the second bucket was not accepted")
    results["second_bucket_after_240"] = {
        "return": second_return, "state": defense.state()}

    defense = Defense(code)
    defense.set_state(anchor=1000, current=199, previous=0, day=0,
                      alarm=0, alarms=0)
    current_return = defense.call(1000)
    _expect(current_return == 0, "current=199 did not refuse packet 200")
    results["current_199"] = {
        "return": current_return, "state": defense.state()}

    defense = Defense(code)
    defense.set_state(anchor=1000, current=8, previous=200, day=0,
                      alarm=0, alarms=0)
    previous_return = defense.call(1000)
    _expect(previous_return == 0, "previous=200 did not block the next bucket")
    results["previous_200"] = {
        "return": previous_return, "state": defense.state()}

    defense = Defense(code)
    defense.set_state(anchor=1000, current=240, previous=240, day=0,
                      alarm=0, alarms=0)
    gap_return = defense.call(260200)
    _expect(gap_return == 0, "a three-bucket gap did not refuse the first packet")
    results["gap_3_buckets"] = {
        "return": gap_return, "state": defense.state()}

    rng = random.Random(3861)
    for _ in range(1000):
        state = {
            "anchor": 1000,
            "previous": rng.randrange(300),
            "current": rng.randrange(300),
            "day": rng.randrange(3),
            "alarm": rng.randrange(2),
            "alarms": rng.randrange(8),
        }
        now = 1000 + rng.randrange(4 * 86400)
        expected_return, expected_state = model(state, now)
        defense = Defense(code)
        defense.set_state(**state)
        actual = (defense.call(now), defense.state())
        _expect(actual == (expected_return, expected_state),
                "instruction execution disagreed with the algorithm model")
    results["model_comparison"] = {
        "cases": 1000,
        "seed": 3861,
        "result": "all matched",
        "scope": (
            "nonnegative elapsed time and ordinary counters; excludes tick "
            "wrap and signed overflow"),
    }
    return results


def _state_text(state: dict[str, int]) -> str:
    return (f"prev={state['previous']} cur={state['current']} "
            f"day={state['day']} alarm={state['alarm']}")


def print_summary(name: str, digest: str, code_count: int, gate_state: str,
                  gate_bytes: str, results: dict) -> None:
    print(f"kernel      {name}")
    print(f"sha256      {digest}")
    print(f"defense     0x{DEFENSE_START:x}-0x{DEFENSE_END:x}, "
          f"{code_count} instructions")
    print(f"gate        0x{GATE_BRANCH:x} {gate_state} ({gate_bytes})")
    print()
    print(f"{'scenario':28s} {'accepted':>8s} {'return':>6s}  state")
    for scenario, result in results.items():
        if "accepted" in result:
            print(f"{scenario:28s} {result['accepted']:>8d} "
                  f"{result['last_return']:>6d}  {_state_text(result['state'])}")
        elif "return" in result:
            print(f"{scenario:28s} {'':>8s} {result['return']:>6d}  "
                  f"{_state_text(result['state'])}")
        else:
            print(f"{scenario:28s} {result['cases']:>8d} cases matched")
    print()
    print("These are instruction-level results, not MCU measurements.")


def load_kernel(args: argparse.Namespace) -> tuple[bytes, str]:
    if args.kernel:
        path = args.kernel
        image = Path(path).read_bytes()
        name = path
    else:
        ota = Ota.read(args.image)
        image = ota.expanded
        name = args.image
    digest = sha256(image)
    if digest not in KNOWN_KERNELS:
        raise Hi3861Error(
            "kernel SHA-256 does not match the documented CP4 2.139 source or "
            "patched image\n"
            f"  expected one of {', '.join(KNOWN_KERNELS)}\n"
            f"  got           {digest}\n"
            "The defense addresses are specific to that build.")
    return image, name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "image", nargs="?",
        help="CP4 Hi3861L OTA (for example Hi3861L_ezviz_sdk_ota.bin)")
    parser.add_argument(
        "--kernel", help="expanded kernel.bin instead of an OTA")
    parser.add_argument(
        "--json", action="store_true", help="print the full result as JSON")
    parser.add_argument(
        "--listing", action="store_true",
        help="print the decoded defense and receiver-gate instructions")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if bool(args.image) == bool(args.kernel):
        parser.error("supply exactly one of IMAGE or --kernel")

    try:
        image, name = load_kernel(args)
        digest = sha256(image)
        gate_state, gate_bytes = _gate_state(image)
        decoder = Decoder(image)
        code = decoder.range(DEFENSE_START, DEFENSE_END)
        if args.listing:
            print_listing(decoder)
        if args.json:
            print(json.dumps({
                "kernel": name,
                "sha256": digest,
                "known_image": KNOWN_KERNELS[digest],
                "gate": {
                    "address": f"0x{GATE_BRANCH:x}",
                    "bytes": gate_bytes,
                    "state": gate_state,
                },
                "defense": {
                    "start": f"0x{DEFENSE_START:x}",
                    "end": f"0x{DEFENSE_END:x}",
                    "instructions": len(code),
                },
                "scenarios": run_scenarios(code),
            }, indent=2))
        else:
            results = run_scenarios(code)
            print_summary(name, digest, len(code), gate_state, gate_bytes,
                          results)
        return 0
    except (Hi3861Error, OSError, ValueError, struct.error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
