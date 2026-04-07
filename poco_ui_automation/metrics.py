from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any

from .models import BusinessMetric, PerformanceSample


class AndroidFrameParsers:
    GFX_TOTAL_RE = re.compile(r"Total frames rendered:\s*(\d+)")
    GFX_JANK_RE = re.compile(r"Janky frames:\s*\d+\s*\(([\d.]+)%\)")
    GFX_P90_RE = re.compile(r"90th percentile:\s*([\d.]+)ms")
    GFX_P95_RE = re.compile(r"95th percentile:\s*([\d.]+)ms")
    GFX_P99_RE = re.compile(r"99th percentile:\s*([\d.]+)ms")

    @classmethod
    def parse_gfxinfo(cls, raw_text: str, scope: str = "gfxinfo") -> PerformanceSample:
        total_frames = cls._search_int(cls.GFX_TOTAL_RE, raw_text)
        jank_ratio = cls._search_float(cls.GFX_JANK_RE, raw_text)
        p90 = cls._search_float(cls.GFX_P90_RE, raw_text)
        p95 = cls._search_float(cls.GFX_P95_RE, raw_text)
        p99 = cls._search_float(cls.GFX_P99_RE, raw_text)
        avg = cls._estimate_avg_frame_ms(p90, p95)
        return PerformanceSample(
            scope=scope,
            avg_frame_ms=avg,
            p90_frame_ms=p90,
            p95_frame_ms=p95,
            p99_frame_ms=p99,
            jank_ratio=jank_ratio,
            total_frames=total_frames,
            raw={"source": "gfxinfo"},
        )

    @classmethod
    def parse_surfaceflinger_latency(
        cls,
        raw_text: str,
        scope: str = "surfaceflinger_latency",
    ) -> PerformanceSample:
        frame_intervals_ms: list[float] = []
        for line in raw_text.splitlines():
            fields = [item.strip() for item in line.split()]
            if len(fields) != 3:
                continue
            try:
                desired = int(fields[0])
                actual = int(fields[1])
            except ValueError:
                continue
            if desired <= 0 or actual <= 0 or actual < desired:
                continue
            frame_intervals_ms.append((actual - desired) / 1_000_000.0)
        if not frame_intervals_ms:
            return PerformanceSample(scope=scope, raw={"source": "surfaceflinger"})
        frame_intervals_ms.sort()
        return PerformanceSample(
            scope=scope,
            avg_frame_ms=sum(frame_intervals_ms) / len(frame_intervals_ms),
            p90_frame_ms=_percentile(frame_intervals_ms, 0.90),
            p95_frame_ms=_percentile(frame_intervals_ms, 0.95),
            p99_frame_ms=_percentile(frame_intervals_ms, 0.99),
            total_frames=len(frame_intervals_ms),
            raw={"source": "surfaceflinger"},
        )

    @staticmethod
    def _search_int(pattern: re.Pattern[str], text: str) -> int:
        match = pattern.search(text)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _search_float(pattern: re.Pattern[str], text: str) -> float | None:
        match = pattern.search(text)
        return float(match.group(1)) if match else None

    @staticmethod
    def _estimate_avg_frame_ms(p90: float | None, p95: float | None) -> float | None:
        if p90 is None and p95 is None:
            return None
        if p90 is None:
            return p95
        if p95 is None:
            return p90
        return (p90 * 0.6) + (p95 * 0.4)


def _percentile(values: list[float], ratio: float) -> float:
    index = max(0, min(len(values) - 1, int(round((len(values) - 1) * ratio))))
    return values[index]


class MetricSampler:
    def __init__(self) -> None:
        self.business_metrics: list[BusinessMetric] = []
        self.performance_samples: list[PerformanceSample] = []

    def record_business_metric(
        self,
        name: str,
        value: Any,
        source: str,
        unit: str = "",
        delta: Any | None = None,
    ) -> BusinessMetric:
        metric = BusinessMetric(name=name, value=value, source=source, unit=unit, delta=delta)
        self.business_metrics.append(metric)
        return metric

    def record_performance_sample(self, sample: PerformanceSample) -> PerformanceSample:
        self.performance_samples.append(sample)
        return sample

    def snapshot(self) -> dict[str, Any]:
        return {
            "business_metrics": [asdict(item) for item in self.business_metrics],
            "performance_samples": [asdict(item) for item in self.performance_samples],
        }
