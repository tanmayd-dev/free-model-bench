"""Ledger -- a tiny, dependency-free personal finance CLI.

Conventions used throughout this project:
  * all money is an integer number of CENTS (negative = expense)
  * dates are `datetime.date`, serialized as YYYY-MM-DD
  * persistence is plain CSV with a header row
  * CLI commands print to stdout and return an int exit code:
        0 = success, 1 = usage/validation error, 2 = policy violation
"""

__version__ = "0.4.1"
