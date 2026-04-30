#!/bin/bash
. ./.venv/bin/activate && python app.py
echo "Virtual envmt: $VIRTUAL_ENV"
python --version
pip list
echo
TIMEOUT=30
echo "EXIT: on my way out in $TIMEOUT sec..." && sleep $TIMEOUT
