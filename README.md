# QASM3 Aer Lab

Desktop tool for testing OpenQASM 3 rewrite compatibility with `qiskit-qasm3-import`, visualizing circuits, and running Aer simulations.

Official repository: https://github.com/GFcyborg/qasm3-to-qiskit

## What the app does

- Left pane (`QASM original`): editable source text
- Top-right (`Qiskit importer`): rewritten importer-compatible QASM and rewrite diagnostics
- Bottom-left (`AST parse-tree`): parsed OpenQASM tree synchronized with editor cursor
- Bottom-right (`Qiskit AER runtime`): rendered circuit and runtime/status output

Current runtime semantics:

- On edit, the app reparses and rewrites automatically.
- If the resulting circuit has no free parameters, simulation is started automatically.
- If the circuit has free parameters, use `Run -> Run manually (w/ params)` (or `Ctrl+R`) to enter parameter values and run.

## Requirements

- Python 3.9+
- Git

Usually enough on Linux:

```bash
sudo apt-get install -y git python3 python3-venv
```

If your platform cannot use prebuilt wheels for scientific dependencies, install build tools as needed (`build-essential`, `cmake`, compiler toolchain).

## Setup (fresh clone)

```bash
git clone https://github.com/GFcyborg/qasm3-to-qiskit.git
cd qasm3-to-qiskit
bash setup.sh
source .venv/bin/activate
python app.py
```

What `setup.sh` does:

- Creates `.venv` if missing
- Activates it
- Upgrades `pip`
- Installs `requirements.txt`
- Verifies key imports (`PySide6`, `qiskit`, `qiskit_aer`, `qiskit_qasm3_import`, `openqasm3`)

Manual setup equivalent:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

## Common actions

- Open examples: `File -> Examples`
- Manual run with parameter prompt: `Run -> Run manually (w/ params)`
- Apply rewrite to editor text: `Run -> Apply rewrite to source`
- Runtime/environment diagnostics: `Run -> Diagnostics`
- Show rewrite rules: `Help -> Rewrite rules`

## Pre-publish checks

Before pushing to GitHub:

```bash
source .venv/bin/activate
python app.py
bash check_portability_paths.sh
```

## Clean-up

```bash
deactivate
rm -rf .venv
git restore .
git clean -fdX
```

Use `git clean -fd` only if you intentionally want to remove non-ignored untracked files too.

## Troubleshooting

- If the project folder is renamed, recreate or reactivate `.venv` from the new path to avoid stale absolute environment paths.
- If parser errors mention ANTLR runtime/type issues, confirm compatible `antlr4-python3-runtime` and generated grammar/runtime versions.

## License

- Main application: GPL-3.0 (see `LICENSE`)
