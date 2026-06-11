"""
Ethical Drift Telemetry - Time-Series Logging for Empirical Analysis

PURPOSE:
Collect time-series data on ethical drift measurements to enable:
1. Convergence analysis (does ||Δη|| decrease over time?)
2. Component correlation (which drift components drive outcomes?)
3. Baseline stability (are baselines converging appropriately?)
4. Empirical validation (do drift measurements predict actual problems?)

This data is essential for patent defensibility - it provides the
"quantitative evidence of superiority" mentioned in the salvage roadmap.

DATA COLLECTED:
- Per-update drift vectors (all 4 components)
- Drift norms and squared norms
- Baseline values at each update
- Decision outcomes (for correlation)
- Timestamps for time-series analysis
"""

import gzip
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import threading

from src.logging_utils import get_logger

logger = get_logger(__name__)

# Thread-safe lock for file operations
_telemetry_lock = threading.Lock()


@dataclass
class DriftSample:
    """Single drift measurement sample."""
    timestamp: str
    agent_id: str

    # Drift components
    calibration_deviation: float
    complexity_divergence: float
    coherence_deviation: float
    stability_deviation: float

    # Computed values
    norm: float
    norm_squared: float

    # Context
    update_count: int
    decision: Optional[str] = None
    confidence: Optional[float] = None

    # Baseline values at this point
    baseline_coherence: Optional[float] = None
    baseline_confidence: Optional[float] = None
    baseline_complexity: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp': self.timestamp,
            'agent_id': self.agent_id,
            'calibration_deviation': self.calibration_deviation,
            'complexity_divergence': self.complexity_divergence,
            'coherence_deviation': self.coherence_deviation,
            'stability_deviation': self.stability_deviation,
            'norm': self.norm,
            'norm_squared': self.norm_squared,
            'update_count': self.update_count,
            'decision': self.decision,
            'confidence': self.confidence,
            'baseline_coherence': self.baseline_coherence,
            'baseline_confidence': self.baseline_confidence,
            'baseline_complexity': self.baseline_complexity,
        }


class DriftTelemetry:
    """
    Telemetry collector for ethical drift measurements.

    Stores time-series data in JSONL format for efficient append-only logging
    and easy analysis with tools like pandas, DuckDB, etc.
    """

    def __init__(self, data_dir: Optional[Path] = None):
        """
        Initialize telemetry collector.

        Args:
            data_dir: Directory for telemetry files. Defaults to data/telemetry/
        """
        if data_dir is None:
            # Use project data directory
            project_root = Path(__file__).parent.parent
            data_dir = project_root / "data" / "telemetry"

        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Main drift telemetry file
        self.drift_file = self.data_dir / "drift_telemetry.jsonl"

        # In-memory buffer for batch writes (reduces I/O)
        self._buffer: List[DriftSample] = []
        self._buffer_size = 10  # Flush after 10 samples

        logger.debug(f"DriftTelemetry initialized: {self.drift_file}")

    def record(
        self,
        drift_vector,  # EthicalDriftVector
        agent_id: str,
        update_count: int,
        baseline=None,  # AgentBaseline
        decision: Optional[str] = None,
        confidence: Optional[float] = None,
    ):
        """
        Record a drift measurement.

        Args:
            drift_vector: The EthicalDriftVector computed for this update
            agent_id: Agent identifier
            update_count: Current update count for the agent
            baseline: AgentBaseline values (optional)
            decision: Decision made (approve/reflect/reject)
            confidence: Confidence level
        """
        sample = DriftSample(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            calibration_deviation=drift_vector.calibration_deviation,
            complexity_divergence=drift_vector.complexity_divergence,
            coherence_deviation=drift_vector.coherence_deviation,
            stability_deviation=drift_vector.stability_deviation,
            norm=drift_vector.norm,
            norm_squared=drift_vector.norm_squared,
            update_count=update_count,
            decision=decision,
            confidence=confidence,
            baseline_coherence=baseline.baseline_coherence if baseline else None,
            baseline_confidence=baseline.baseline_confidence if baseline else None,
            baseline_complexity=baseline.baseline_complexity if baseline else None,
        )

        self._buffer.append(sample)

        # Flush if buffer is full
        if len(self._buffer) >= self._buffer_size:
            self.flush()

    def flush(self):
        """Flush buffer to disk."""
        if not self._buffer:
            return

        with _telemetry_lock:
            try:
                with open(self.drift_file, 'a') as f:
                    for sample in self._buffer:
                        f.write(json.dumps(sample.to_dict()) + '\n')
                self._buffer = []
            except Exception as e:
                logger.error(f"Failed to flush drift telemetry: {e}")

    def get_recent(self, agent_id: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """
        Get recent telemetry samples.

        Args:
            agent_id: Filter by agent (optional)
            limit: Maximum samples to return

        Returns:
            List of sample dictionaries, most recent first
        """
        # Flush buffer first
        self.flush()

        samples = []

        if not self.drift_file.exists():
            return samples

        try:
            with open(self.drift_file, 'r') as f:
                lines = f.readlines()

            # Read in reverse (most recent first)
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    sample = json.loads(line)
                    if agent_id is None or sample.get('agent_id') == agent_id:
                        samples.append(sample)
                        if len(samples) >= limit:
                            break
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.error(f"Failed to read drift telemetry: {e}")

        return samples

    def get_statistics(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get aggregate statistics from telemetry data.

        Returns:
            Statistics dictionary with mean, std, trends, etc.
        """
        samples = self.get_recent(agent_id=agent_id, limit=1000)

        if not samples:
            return {
                'total_samples': 0,
                'agents': [],
                'message': 'No telemetry data available',
            }

        # Aggregate statistics
        norms = [s['norm'] for s in samples]
        cal_devs = [s['calibration_deviation'] for s in samples]
        cpx_divs = [s['complexity_divergence'] for s in samples]
        coh_devs = [s['coherence_deviation'] for s in samples]
        stab_devs = [s['stability_deviation'] for s in samples]

        agents = list(set(s['agent_id'] for s in samples))

        def safe_mean(lst):
            return sum(lst) / len(lst) if lst else 0.0

        def safe_std(lst):
            if len(lst) < 2:
                return 0.0
            mean = safe_mean(lst)
            variance = sum((x - mean) ** 2 for x in lst) / len(lst)
            return variance ** 0.5

        return {
            'total_samples': len(samples),
            'agents': agents,
            'agent_count': len(agents),
            'norm': {
                'mean': safe_mean(norms),
                'std': safe_std(norms),
                'min': min(norms) if norms else 0.0,
                'max': max(norms) if norms else 0.0,
            },
            'components': {
                'calibration_deviation': {
                    'mean': safe_mean(cal_devs),
                    'std': safe_std(cal_devs),
                },
                'complexity_divergence': {
                    'mean': safe_mean(cpx_divs),
                    'std': safe_std(cpx_divs),
                },
                'coherence_deviation': {
                    'mean': safe_mean(coh_devs),
                    'std': safe_std(coh_devs),
                },
                'stability_deviation': {
                    'mean': safe_mean(stab_devs),
                    'std': safe_std(stab_devs),
                },
            },
            # Trend analysis: compare first half vs second half
            'trend': {
                'improving': safe_mean(norms[:len(norms)//2]) > safe_mean(norms[len(norms)//2:])
                if len(norms) >= 10 else None,
            },
        }

    def rotate(self, max_size_mb: float = 100.0, archive_months: int = 12) -> Optional[Path]:
        """
        Rotate drift telemetry file if it exceeds max_size_mb.

        Archives to data/telemetry/archive/ as gzipped JSONL.
        Returns the archive path if rotation happened, None otherwise.
        """
        if not self.drift_file.exists():
            return None

        size_mb = self.drift_file.stat().st_size / (1024 * 1024)
        if size_mb < max_size_mb:
            return None

        archive_dir = self.data_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rotated = self.data_dir / f"drift_telemetry_{stamp}.jsonl"
        archive_path = archive_dir / f"drift_telemetry_{stamp}.jsonl.gz"

        with _telemetry_lock:
            # Flush any buffered samples first
            if self._buffer:
                try:
                    with open(self.drift_file, 'a') as f:
                        for sample in self._buffer:
                            f.write(json.dumps(sample.to_dict()) + '\n')
                    self._buffer = []
                except Exception as e:
                    logger.error(f"Failed to flush before rotation: {e}")

            # Rename current file
            self.drift_file.rename(rotated)
            # Create fresh empty file
            self.drift_file.touch()

        # Gzip outside the lock (can take time for large files)
        try:
            with open(rotated, 'rb') as f_in:
                with gzip.open(archive_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            rotated.unlink()
            logger.info(f"Rotated drift telemetry ({size_mb:.0f}MB) -> {archive_path}")
        except Exception as e:
            logger.error(f"Failed to gzip rotated telemetry: {e}")
            return rotated  # Return uncompressed path

        # Prune old archives
        self._prune_archives(archive_dir, archive_months)

        return archive_path

    @staticmethod
    def _prune_archives(archive_dir: Path, keep_months: int):
        """Remove archives older than keep_months."""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=keep_months * 30)
        for gz_file in sorted(archive_dir.glob("drift_telemetry_*.jsonl.gz")):
            try:
                # Parse date from filename: drift_telemetry_YYYYMMDD_HHMMSS.jsonl.gz
                date_str = gz_file.stem.replace("drift_telemetry_", "").replace(".jsonl", "")
                file_date = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
                if file_date < cutoff:
                    gz_file.unlink()
                    logger.info(f"Pruned old archive: {gz_file.name}")
            except (ValueError, OSError):
                pass

    def export_csv(self, output_path: Optional[Path] = None, agent_id: Optional[str] = None) -> Path:
        """
        Export telemetry data to CSV for analysis.

        Args:
            output_path: Output file path (default: telemetry/drift_export.csv)
            agent_id: Filter by agent (optional)

        Returns:
            Path to exported CSV file
        """
        if output_path is None:
            output_path = self.data_dir / "drift_export.csv"

        samples = self.get_recent(agent_id=agent_id, limit=10000)

        if not samples:
            raise ValueError("No telemetry data to export")

        # Write CSV
        headers = list(samples[0].keys())

        with open(output_path, 'w') as f:
            f.write(','.join(headers) + '\n')
            for sample in samples:
                row = [str(sample.get(h, '')) for h in headers]
                f.write(','.join(row) + '\n')

        logger.info(f"Exported {len(samples)} samples to {output_path}")
        return output_path


# Singleton instance
_telemetry: Optional[DriftTelemetry] = None


def get_telemetry() -> DriftTelemetry:
    """Get the singleton telemetry instance."""
    global _telemetry
    if _telemetry is None:
        _telemetry = DriftTelemetry()
    return _telemetry


def record_drift(
    drift_vector,
    agent_id: str,
    update_count: int,
    baseline=None,
    decision: Optional[str] = None,
    confidence: Optional[float] = None,
):
    """Convenience function to record drift measurement."""
    get_telemetry().record(
        drift_vector=drift_vector,
        agent_id=agent_id,
        update_count=update_count,
        baseline=baseline,
        decision=decision,
        confidence=confidence,
    )
