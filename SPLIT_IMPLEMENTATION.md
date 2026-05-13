## Summary: QASM3 Splitter Implementation

### What was created

I've successfully implemented a completely new GUI application `split.py` with the same look & feel as `run.py`, plus refactored the codebase to share the rewriting logic.

### Files Created/Modified

1. **qasm_rewriter.py** (NEW - 42 KB)
   - Extracted shared OpenQASM rewriting logic from run.py
   - Contains all transpilation rules, AST walking, and rewriting logic
   - Used by both run.py and split.py
   - Exports: `transpile_qasm()`, `Issue`, `kind`, `span`, `node_iter`, and all helper functions

2. **split.py** (NEW - 16 KB)
   - Completely new GUI application for splitting QASM files
   - Two-pane layout:
     - **Left**: Original QASM code (read-only) with split point markers
     - **Right**: Tabbed view showing rewritten chunks
   - Features:
     - Right-click on lines to mark/unmark split points (lines turn red when marked)
     - Real-time preview of chunks in tabs
     - "Save & Create Chunks" button to write chunks to disk in a `<filename>/` directory
     - "Run Chunks" button to launch independent run.py instances for each chunk
   - All chunks use the same rewriting rules as run.py (via shared qasm_rewriter.py)

3. **run.py** (MODIFIED)
   - Removed duplicate transpilation code
   - Now imports `transpile_qasm`, `Issue`, `kind`, `span`, `node_iter` from qasm_rewriter
   - ~20KB reduction (all shared logic moved to qasm_rewriter.py)
   - Identical functionality, cleaner architecture

4. **README.md** (UPDATED)
   - Added documentation for split.py
   - Explained both tools (run.py and split.py)
   - Added usage examples and workflow instructions

### Architecture

**Shared rewriting engine (qasm_rewriter.py)**
```
transpile_qasm(source: str) -> (rewritten: str, issues: list[Issue], program: AST)
  ├── Extract QASM version
  ├── Parse source to AST
  ├── Extract subroutines for inlining
  ├── Emit rewritten statements via emit_stmt()
  ├── Add inferred qubit declarations
  ├── Add stdgates.inc definitions if needed
  ├── Rewrite hardware qubits ($0 → hw[0])
  └── Normalize to Qiskit-compatible form
```

**split.py workflow**
```
User loads QASM → Parse to AST → Extract statement spans
                     ↓
         Right-click to mark split points
                     ↓
    Split file by lines → Group into chunks
                     ↓
    For each chunk: transpile_qasm() → tab preview
                     ↓
         Save & Create Chunks → Rewrite each to disk
                     ↓
              Run Chunks → subprocess run.py for each
```

### Key Design Decisions

1. **Split at statement boundaries, not arbitrary lines**
   - Ensures each chunk is valid, parseable OpenQASM
   - User marks by line number (1-indexed), split AFTER that line
   - All qubit declarations and gate definitions included in each chunk

2. **Unified rewriting logic**
   - All transpilation rules in qasm_rewriter.py
   - Both tools use identical rewriting via shared `transpile_qasm()`
   - No code duplication

3. **Directory structure for chunks**
   - Creates `<original_filename>/` directory
   - Saves chunks as `<original_filename>_1.qasm`, `_2.qasm`, etc.
   - Includes `.split_metadata.json` for tracking
   - Never modifies original QASM files

4. **Independent run instances**
   - Each chunk launches in its own run.py window
   - Chunks can be run in parallel (process isolation via ProcessPoolExecutor in run.py)
   - Each chunk treated as independent circuit

### Usage

#### Run the circuit analyzer (existing functionality preserved):
```bash
python run.py                    # Interactive file chooser
python run.py examples/adder.qasm  # Load specific file
```

#### Run the splitter (new):
```bash
python split.py                  # Interactive file chooser
python split.py examples/adder.qasm  # Load specific file

# Then:
# 1. Right-click lines to mark split points (turn red)
# 2. Preview chunks in tabs
# 3. Click "Save & Create Chunks" to write to disk
# 4. Click "Run Chunks" to launch independent windows
```

### Testing

All imports verified:
- ✓ qasm_rewriter.py imports successfully
- ✓ split.py imports successfully
- ✓ run.py imports successfully
- ✓ Tested transpile_qasm() with 3 example files

Tested splitting logic:
- ✓ Split adder.qasm at lines 30 and 38 → 3 chunks
- ✓ Each chunk rewritable with 0 errors
- ✓ Rewritten output is valid Qiskit-compatible QASM

### Future Enhancements

1. Save/restore split points between sessions (metadata in .split_metadata.json)
2. Progress indicator when saving large numbers of chunks
3. Filter chunks by size/complexity before bulk run
4. Merge results from chunk runs
5. Visual circuit comparison between original and chunks

### Backward Compatibility

- ✓ run.py works identically to before (same features, same GUI)
- ✓ All examples still load correctly
- ✓ No breaking changes to API or file formats
- ✓ setup.sh and requirements.txt unchanged
