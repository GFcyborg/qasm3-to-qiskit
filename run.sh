#!/bin/bash
. ./.venv/bin/activate && python run.py "$@"
echo "Virtual envmt: $VIRTUAL_ENV"
python --version
pip list
echo
TIMEOUT=5
echo "EXIT: on my way out in $TIMEOUT sec..." && sleep $TIMEOUT
