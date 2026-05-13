## Quick Start Guide

### Setup (one-time)
```bash
# Activate virtual environment
source .venv/bin/activate

# Ensure dependencies are installed
pip install -r requirements.txt
```

### Run the Circuit Analyzer (existing app)
```bash
# Interactive: choose file via dialog
python run.py

# Direct: load specific file
python run.py examples/bell_state.qasm

# Features:
# - Visualize circuit structure and gates
# - Simulate with Aer backend
# - Analyze rewriting issues
# - Export circuit diagrams
```

### Run the Splitter (new app)
```bash
# Interactive: choose file via dialog
python split.py

# Direct: load specific file
python split.py examples/adder.qasm

# Workflow:
# 1. File loads in left pane (read-only)
# 2. Right-click on line numbers to mark split points (turn red)
# 3. Chunks appear in tabs on right (automatically rewritten)
# 4. "Save & Create Chunks" → saves to <filename>/ directory
# 5. "Run Chunks" → launches run.py for each chunk separately
```

### Example: Analyzing a 3-Stage Adder

**Step 1: Load the file**
```bash
python split.py examples/adder.qasm
```

**Step 2: Mark split points**
- Right-click line 27 (after declarations) → "Add split after line 27"
- Right-click line 34 (after setup) → "Add split after line 34"
- Lines turn red to show splits

**Step 3: Review chunks**
- Tab 1: Declarations and reset
- Tab 2: Input initialization
- Tab 3: Main computation

**Step 4: Save to disk**
- Click "Save & Create Chunks"
- Creates `adder/` directory:
  ```
  adder/
  ├── adder_1.qasm
  ├── adder_2.qasm
  ├── adder_3.qasm
  └── .split_metadata.json
  ```

**Step 5: Run stages independently**
- Click "Run Chunks"
- 3 separate run.py windows launch
- Each shows its stage's circuit and simulation

### What's New vs What's Unchanged

**New**
- `split.py` - Splitter application
- `qasm_rewriter.py` - Shared rewriting engine
- `SPLIT_IMPLEMENTATION.md` - Technical docs
- `SPLIT_WORKFLOWS.md` - Usage examples

**Updated**
- `run.py` - Now uses qasm_rewriter.py (cleaner, no duplication)
- `README.md` - Includes split.py documentation

**Unchanged**
- `setup.sh`, `requirements.txt`, `run.py`
- All examples in `examples/`
- Grammar files in `grammar/`

### Common Operations

**Split a file by functionality**
```
Run split.py → Mark splits at function boundaries → Save chunks
```

**Debug one section at a time**
```
Run split.py → Mark splits around problem area → Run that chunk with run.py
```

**Compare pre/post rewriting**
```
Run split.py → Load file → Check chunk tabs → See rewritten output immediately
```

**Batch save all chunks**
```
Run split.py → Mark many splits → Click "Save & Create Chunks" once
→ All chunks written to disk in seconds
```

### Troubleshooting

| Issue | Solution |
|-------|----------|
| "No module named 'qasm_rewriter'" | Ensure you're in the workspace root directory with the active venv |
| split.py won't open file | File may be corrupted; try a known-good example like `examples/bell_state.qasm` |
| Chunks show as empty tabs | No splits marked yet; right-click line numbers to mark splits |
| run.py window won't open | Port conflict or display issue; try running one chunk at a time |

### File Locations

```
workspace/
├── split.py                # ← Run this for splitting
├── run.py                  # ← Run this for analysis
├── qasm_rewriter.py        # ← Shared logic (both use this)
├── README.md
├── SPLIT_IMPLEMENTATION.md
├── SPLIT_WORKFLOWS.md
├── examples/
│   ├── adder.qasm
│   ├── bell_state.qasm
│   ├── teleport.qasm
│   └── ... (20+ examples)
└── .venv/                  # Python environment
```

### Performance Estimates

- **Load file into split.py**: ~0.5s
- **Preview chunks**: ~1s total (all chunks transpiled)
- **Save chunks**: ~0.5s (disk I/O)
- **Launch run.py window**: ~2-5s per window
- **Simulate circuit**: Depends on gate count (see run.py diagnostics)

### Next Steps

1. **Try the examples**: `python split.py examples/adder.qasm`
2. **Read workflows**: See `SPLIT_WORKFLOWS.md` for detailed scenarios
3. **Check implementation**: See `SPLIT_IMPLEMENTATION.md` for architecture
4. **Review source**: See `qasm_rewriter.py` for the rewriting logic

### Tips

- Use `File → Examples` menu in split.py to quickly load standard circuits
- Red line numbers in split.py show where splits will occur
- Each chunk includes full qubit declarations (independent execution)
- Chunks preserve original QASM comments and formatting where possible
- Try splitting at comment lines to see the separation clearly

---

**For help with quantum circuits**, see the built-in help in run.py (Help menu).

**For OpenQASM 3 syntax**, refer to `examples/` and the grammar in `grammar/`.

**For rewriting rules**, see functions in `qasm_rewriter.py` (constants like `STDGATES_COMPAT_GATES`, `UNMEASURABLE_GATES`, etc.).
