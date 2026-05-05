# Contributing to QASM3 Aer Lab

Keep changes small, tested, and aligned with the existing style.

## Getting Started

1. **Fork the official repository** on GitHub: https://github.com/GFcyborg/qasm3-to-qiskit
2. **Clone your fork** locally and set up the environment:

   ```bash
   git clone https://github.com/YOUR-USERNAME/qasm3-to-qiskit.git
   cd qasm3-to-qiskit
   git remote add upstream https://github.com/GFcyborg/qasm3-to-qiskit.git
   bash setup.sh
   source .venv/bin/activate
   ```

3. **Create a feature branch**:

   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Workflow

1. Make your changes.
2. Test with the app:

   ```bash
   python run.py
   ```

3. Keep the code style consistent.
4. Commit with a clear message:

   ```bash
   git commit -m "Add feature: brief description"
   ```

## Code Style

- Follow PEP 8 for Python code
- Use type hints for function parameters and return values
- Add docstrings to public functions and classes

## Testing

- Test your changes with multiple examples
- Verify circuit diagrams render correctly
- Check that Aer simulations complete without errors

## Submitting a Pull Request

1. **Push your branch** to your fork:

   ```bash
   git push origin feature/your-feature-name
   ```

2. **Open a Pull Request** with a clear title and short description.
3. **Respond to review feedback**.

## Reporting Issues

When reporting bugs:

- **Describe the problem** clearly
- **Provide steps to reproduce**
- **Include error messages** (screenshots or logs)
- **Specify your environment** (OS, Python version, etc.)

## Questions?

- Review the README.md for usage documentation
- Look at existing examples in the `examples/` directory

## License

By contributing, you agree that your contributions will be licensed under the same license as the project.
