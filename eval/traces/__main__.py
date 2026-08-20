"""
 `python -m eval.traces` 
"""

import sys

from eval.traces import runs, summary

args = sys.argv[1:]
directory = args[1] if len(args) > 1 else None

if args:
    print(summary(args[0], directory))
else:
    for run_id in runs(directory):
        print(run_id)
