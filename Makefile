# On Windows without make, run the python commands directly (see README).
PY ?= python3
export PYTHONPATH := src:tests

test:
	$(PY) -m unittest discover -s tests -v

eval:
	$(PY) eval/runner/run_tasks.py
