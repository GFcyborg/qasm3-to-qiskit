## Split.py Workflow Examples

### Example 1: Split the Adder Circuit

**Goal**: Analyze three stages of the adder separately

```
1. Open split.py
2. File -> Examples -> adder.qasm
3. Right-click line 27 (after qubit declarations) -> "Add split after line 27"
4. Right-click line 34 (after input setup) -> "Add split after line 34"
5. Preview tabs show:
   - Chunk 1: Qubit declarations and reset
   - Chunk 2: Input initialization loop
   - Chunk 3: Main addition and measurement
6. Click "Save & Create Chunks"
   → Creates adder/ directory with:
     - adder_1.qasm (declarations + setup)
     - adder_2.qasm (input stage)
     - adder_3.qasm (computation stage)
     - .split_metadata.json (tracking file)
7. Click "Run Chunks"
   → Launches 3 independent run.py windows
   → Each window shows circuit visualization and simulation
```

### Example 2: Break Larger Algorithm at Control Flow Points

**Goal**: Isolate quantum subroutine calls

```
File: teleport.qasm

Split strategy:
- Line 5 (after include) 
- Line 14 (after qubit/bit declarations)
- Line 19 (after first measurement)

Results in chunks:
1. Headers + declarations
2. Bell pair preparation
3. Measurement and correction
```

### Example 3: Multi-Stage Quantum Error Correction

**Goal**: Debug QEC code stage by stage

```
File: qec.qasm (hypothetically large)

1. Mark after each major section (gates, prep, encoding, syndrome, correction)
2. Each chunk can be:
   - Visualized independently
   - Checked for rewriting issues
   - Simulated with different parameters
   - Tested at each stage without re-running full circuit
```

### Example 4: Comparative Analysis with run.py

**Workflow A - Traditional (run.py)**
```
run.py → Load file → Edit locally → Single transpile → Visualize entire circuit
```

**Workflow B - Staged (split.py + run.py)**
```
split.py → Load file → Mark splits → Preview chunks
         → Save chunks → Launch separate run.py instances
         → Compare circuits side-by-side
         → Analyze each stage independently
```

## Implementation Details

### What Happens When You Save Chunks

For file `examples/teleport.qasm` with splits at lines 10 and 20:

1. **Directory created**: `examples/teleport/`
2. **Chunk files saved**:
   ```
   teleport/
   ├── teleport_1.qasm    # Lines 1-10 (rewritten)
   ├── teleport_2.qasm    # Lines 11-20 (rewritten)
   ├── teleport_3.qasm    # Lines 21-end (rewritten)
   └── .split_metadata.json
   ```

3. **Each chunk contains**:
   - OPENQASM 3.0 header (added if missing)
   - Inferred qubit declarations
   - Expanded stdgates.inc (if needed)
   - Rewritten statements from that section
   - All gate definitions from original (duplicated in each)

### Split Point Semantics

- **"Split after line N"** means: after line N ends, start a new chunk
- Lines are **1-indexed** (display: line 1-44)
- Can mark up to 42 splits in a 44-line file (though that's silly)
- Red highlight in line number area shows marked splits
- Original lines preserved exactly; chunks are rewritten copies

### Error Handling

- **Invalid split**: If user marks no splits, saves entire file as single chunk
- **Unparseable chunk**: Chunk still saved, but marked with error in run.py window
- **Missing includes**: Automatically expanded/inlined as needed
- **Unmeasurable statements**: Dropped with diagnostic notes (same as run.py)

## Performance Notes

- **Transpilation**: ~50-200ms per chunk (depending on complexity)
- **File I/O**: ~10-50ms for saving 10 chunks
- **run.py instances**: Each takes ~2-5s to launch GUI and parse
- **Simulation**: Depends on circuit size (see run.py diagnostics)

For a 1000-statement file:
- Split into 10 chunks: ~2s to preview, ~0.5s to save, ~50s to launch all
- Each chunk: ~100 statements, individually runnable

## Tips & Tricks

1. **Find statement boundaries visually**
   - Use run.py's AST pane to understand structure
   - Mark splits between major sections (declarations, initialization, computation)

2. **Reuse split patterns**
   - Common pattern: splits after qubit declarations, after gate definitions, before measurement
   - Metadata file `.split_metadata.json` can be used for future sessions

3. **Combine split.py with version control**
   ```bash
   cd <filename>/  # Enter chunk directory
   git init        # Track chunk rewrites
   git add *.qasm
   git commit -m "Chunk splits for stage analysis"
   ```

4. **Batch testing**
   - Split multiple files, save all chunks
   - Use shell loop: `for f in */*.qasm; do python run.py "$f"; done`

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| Can't mark splits | No file loaded | Click "Open..." or "Examples" first |
| Chunks won't save | Directory exists? | Manual permission issue; try with other directory |
| run.py won't launch | Path wrong | Check .split_metadata.json has correct paths |
| Chunk shows parse error | Incomplete code | Verify split point includes all dependencies |
| Rewritten chunks differ from original | Expected | Rewriter normalizes syntax (this is intentional) |

## Advanced: Programmatic Splitting

If you want to automate splitting without the GUI:

```python
from pathlib import Path
from qasm_rewriter import transpile_qasm

# Read file
file = Path('large_algorithm.qasm')
text = file.read_text()

# Define splits (1-indexed line numbers after which to split)
splits = {30, 60, 90}  # 3 chunks

# Manual split
lines = text.splitlines(keepends=True)
chunks = []
chunk = []
for i, line in enumerate(lines, 1):
    chunk.append(line)
    if i in splits:
        chunks.append(''.join(chunk))
        chunk = []
if chunk:
    chunks.append(''.join(chunk))

# Rewrite chunks
out_dir = file.parent / file.stem
out_dir.mkdir(exist_ok=True)
for i, chunk_text in enumerate(chunks, 1):
    rewritten, _, _ = transpile_qasm(chunk_text)
    (out_dir / f"{file.stem}_{i}.qasm").write_text(rewritten)
```

This is what split.py does internally!
