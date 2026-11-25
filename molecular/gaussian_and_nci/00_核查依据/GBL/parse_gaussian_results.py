# -*- coding: utf-8 -*-
import csv
import re
from pathlib import Path

rows = []
for path in sorted((Path(__file__).parent / "gaussian").glob("*.log")):
    text = path.read_text(encoding="utf-8", errors="replace")
    scf = re.findall(r"SCF Done:\s+E\([^)]*\)\s+=\s+(-?\d+\.\d+)", text)
    cp = re.findall(r"Counterpoise corrected energy\s+=\s+(-?\d+\.\d+)", text, flags=re.I)
    rows.append({
        "log_file": str(path),
        "scf_energy_hartree": scf[-1] if scf else "",
        "counterpoise_corrected_hartree": cp[-1] if cp else "",
        "status": "parsed" if (scf or cp) else "no_energy_found",
    })

out = Path(__file__).parent / "gaussian_parsed_results.csv"
with out.open("w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=["log_file", "scf_energy_hartree", "counterpoise_corrected_hartree", "status"])
    writer.writeheader()
    writer.writerows(rows)
print(out)
