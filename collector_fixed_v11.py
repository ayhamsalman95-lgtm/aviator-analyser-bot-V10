"""Compatibility shim: the V11 collector was replaced by aviator/collector.py (V12)."""
from aviator.collector import main

if __name__ == "__main__":
    main()
