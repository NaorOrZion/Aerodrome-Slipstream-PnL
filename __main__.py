"""
Entry point for the Slipstream PnL analyzer.

Usage:
    python -m Aerodrome.slipstream_pnl
"""

from .analyzer import run_analysis

if __name__ == "__main__":
    run_analysis()
