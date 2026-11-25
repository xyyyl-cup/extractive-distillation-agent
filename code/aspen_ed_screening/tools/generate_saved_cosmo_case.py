import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from thermo_regression_agent import Component, Evidence, RegressionSpec, ThermoCaseSpec, generate_case, split_indices

src = Path(sys.argv[1])
out = Path(sys.argv[2])
rows = []
for line in src.read_text(encoding='latin-1').splitlines():
    parts = line.split()
    if len(parts) >= 11:
        try:
            x, t, y = float(parts[0]), float(parts[4]), float(parts[9])
        except ValueError:
            continue
        if 0.001 <= x <= 0.999:
            rows.append({'T_K': t, 'P_BAR': 1.01325, 'x1': x, 'y1': y})
train, holdout = split_indices(len(rows), holdout_every=3)
case = ThermoCaseSpec(
    case_id='c3f8_c3f6_cosmo_saved_nrtl',
    components=[Component('C3F8', 'C3F8', 'OCTAFLUOROPROPANE'), Component('C3F6', 'C3F6', 'HEXAFLUOROPROPYLENE')],
    selected_model='NRTL', data_type='TPXY', data_rows=rows,
    training_indices=train, holdout_indices=holdout,
    regression=RegressionSpec('NRTL', 4, [0, 0, 0, 0], [-10000]*4, [10000]*4),
    evidence=Evidence('cosmors_surrogate_regression', str(src), True, {'cosmors_file_sha256': 'saved-tab'}),
    output_dir=str(out),
)
print(generate_case(case))
