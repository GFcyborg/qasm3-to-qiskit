#!/bin/bash

# Setup script for QASM3 Aer Lab
# Automated setup: installs Python dependencies

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "QASM3 Aer Lab - Setup"
echo "=========================================="
echo ""

echo "[1/2] Setting up Python environment..."

# Step 2: Create Python virtual environment
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$SCRIPT_DIR/.venv"
fi

# Activate virtual environment
source "$SCRIPT_DIR/.venv/bin/activate"

echo "Upgrading pip..."
pip install --upgrade pip

echo "Installing dependencies from requirements.txt..."
pip install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "[2/2] Verifying installation..."

if python -c "import PySide6, qiskit, qiskit_aer, qiskit_qasm3_import, openqasm3" 2>/dev/null; then
    echo "✓ Python dependencies installed"
else
    echo "✗ Python dependencies missing"
    exit 1
fi

echo ""
echo "=========================================="
echo "✓ Setup complete!"
echo "=========================================="
echo ""
echo "To run the application:"
echo "  1. Activate the environment:"
echo "     source .venv/bin/activate"
echo "  2. Start the GUI:"
echo "     python app.py"
echo ""
echo "For more information, see README.md"
echo ""
