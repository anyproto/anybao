#!/usr/bin/env python3
"""Verify the pinned ARM64 K2 load instruction and Wasmtime 49.0.3 trap mapping."""

import argparse
import hashlib
import json
import struct
import subprocess
from pathlib import Path


def sections(data):
    if data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != 183:
        raise ValueError("expected little-endian ARM64 ELF")
    offset = struct.unpack_from("<Q", data, 40)[0]
    size, count, names_index = struct.unpack_from("<HHH", data, 58)
    headers = [struct.unpack_from("<IIQQQQIIQQ", data, offset + i * size) for i in range(count)]
    names_header = headers[names_index]
    names = data[names_header[4] : names_header[4] + names_header[5]]
    return {names[h[0] :].split(b"\0", 1)[0].decode(): data[h[4] : h[4] + h[5]] for h in headers}


def traps(section):
    # Pinned wasmtime-environ 49.0.3 trap_encoding.rs / iterate_traps format.
    count, blocks = struct.unpack_from("<II", section)
    bodies = 8 + blocks * 8
    output = {}
    for block in range(blocks):
        pc, position = struct.unpack_from("<II", section, 8 + block * 8)
        position += bodies
        default_code = section[position]
        position += 1
        for _ in range(min(128, count - block * 128)):
            token, shift = 0, 0
            while True:
                byte = section[position]
                position += 1
                token |= (byte & 127) << shift
                if byte < 128:
                    break
                shift += 7
            pc += token >> 1
            code = default_code
            if token & 1:
                code = section[position]
                position += 1
            output[pc] = code
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--objdump", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = args.artifact.read_bytes()
    manifest = json.loads(args.manifest.read_text())
    sha = hashlib.sha256(data).hexdigest()
    if sha != manifest["artifact_sha256"] or manifest["wasmtime_version"] != "49.0.3":
        raise SystemExit("artifact hash or Wasmtime version mismatch")
    elf = sections(data)
    mapping = traps(elf[".wasmtime.traps"])
    expected = {
        0x74: 0xB27E6BE1,  # mov x1, #0x1ffffffc (reservation - access width)
        0x7C: 0x8B244042,  # add x2, x2, w4, uxtw (base + input address)
        0x84: 0xEB01001F,  # cmp x0, x1
        0x88: 0x9A828060,  # csel x0, x3, x2, hi (null only outside reservation)
        0x8C: 0xB9400002,  # ldr w2, [x0] (actual faulting load)
    }
    verified = (
        all(
            struct.unpack_from("<I", elf[".text"], pc)[0] == instruction
            for pc, instruction in expected.items()
        )
        and mapping.get(0x8C) == 1
    )
    args.output.mkdir(parents=True, exist_ok=True)
    disassembly = subprocess.check_output([args.objdump, "-d", str(args.artifact)], text=True)
    (args.output / "traps-arm64-disassembly.txt").write_text(disassembly)
    result = {
        "scope": "static_generated_artifact_evidence_only",
        "artifact_sha256": sha,
        "source_sha256": manifest["source_sha256"],
        "profile_sha256": manifest["profile_sha256"],
        "wasmtime_version": "49.0.3",
        "verified": verified,
        "faulting_text_offset": "0x8c",
        "instruction": "ldr w2, [x0]",
        "mapped_trap": "MemoryOutOfBounds",
        "trap_mapping": {hex(pc): code for pc, code in mapping.items()},
        "memory_length_bytes": 65536,
        "input_addresses": [65536 + 4 * n for n in range(40)],
        "reservation_bytes": 536870912,
        "guard_bytes": 65536,
        "explanation": (
            "All inputs select base+address, within the reservation and beyond the committed "
            "memory. The mapped LDR must fault; no explicit bounds-check trap is taken."
        ),
        "android_execution_verified": None,
        "k2_verdict": "pending_phone_200_cases_and_owner_watchdog",
    }
    (args.output / "trap-signal-evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"verified_static_fault_mapping": verified, "artifact_sha256": sha}))
    if not verified:
        raise SystemExit(
            "INCONCLUSIVE: generated instructions changed; inspect before claiming signal path"
        )


if __name__ == "__main__":
    main()
