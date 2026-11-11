"""QuantLab: research, signals and paper trading for US and ASX markets."""
import os

# joblib (used by scikit-learn) probes for the physical core count with a Windows tool
# that newer Windows versions no longer ship, and prints a long warning when it fails.
# Telling it the core count up front skips the probe.
os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 1))

__version__ = "0.1.0"
