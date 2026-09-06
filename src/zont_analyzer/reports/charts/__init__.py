"""Standalone, evidence-preserving SVG charts for report exports.

The renderer intentionally consumes only timestamped observations supplied in a
report's optional ``context.chart_series`` packet.  Temporal-evidence window
statistics are not a time series and must never be turned into a line chart.
"""

from .render import render_charts

__all__ = ["render_charts"]
