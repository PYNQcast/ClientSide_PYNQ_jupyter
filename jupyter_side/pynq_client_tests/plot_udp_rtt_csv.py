#!/usr/bin/env python3
"""Plot direct UDP RTT CSV captures for notebooks or PNG export."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def load_rtt_rows(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        csv_path = Path(path)
        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                parsed = dict(row)
                parsed["_label"] = row.get("label") or csv_path.stem
                parsed["sample_index"] = int(row["sample_index"]) if row.get("sample_index") else None
                parsed["seq"] = int(row["seq"]) if row.get("seq") else None
                parsed["rtt_ms"] = float(row["rtt_ms"]) if row.get("rtt_ms") else None
                rows.append(parsed)
    return rows


def summarise_by_label(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["_label"]), []).append(row)

    summary = []
    for label, group in grouped.items():
        ok_values = [float(row["rtt_ms"]) for row in group if row.get("status") == "ok" and row["rtt_ms"] is not None]
        lost = sum(1 for row in group if row.get("status") != "ok")
        ordered = sorted(ok_values)
        p95 = ordered[max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))] if ordered else 0.0
        summary.append({
            "label": label,
            "count": len(group),
            "samples_ok": len(ok_values),
            "samples_lost": lost,
            "loss_pct": (lost / len(group) * 100.0) if group else 0.0,
            "avg_rtt_ms": (sum(ok_values) / len(ok_values)) if ok_values else 0.0,
            "p95_rtt_ms": p95,
            "max_rtt_ms": max(ok_values) if ok_values else 0.0,
        })
    summary.sort(key=lambda item: item["label"])
    return summary


def plot_rtt_rows(rows: list[dict], out_dir: str | None = None):
    import matplotlib.pyplot as plt

    summary = summarise_by_label(rows)
    labels = [item["label"] for item in summary]
    avg_rtt = [item["avg_rtt_ms"] for item in summary]
    p95_rtt = [item["p95_rtt_ms"] for item in summary]
    loss_pct = [item["loss_pct"] for item in summary]

    figures = []

    fig, ax = plt.subplots(figsize=(9, 5))
    x = range(len(labels))
    width = 0.38
    ax.bar([i - width / 2 for i in x], avg_rtt, width=width, label="Avg RTT (ms)")
    ax.bar([i + width / 2 for i in x], p95_rtt, width=width, label="P95 RTT (ms)")
    ax.set_title("Direct UDP RTT by Load")
    ax.set_ylabel("Milliseconds")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.legend()
    fig.tight_layout()
    figures.append(("udp_rtt_comparison.png", fig))

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(labels, loss_pct, color="#d95f02")
    ax.set_title("UDP RTT Timeout Rate")
    ax.set_ylabel("Loss %")
    ax.tick_params(axis="x", rotation=20)
    fig.tight_layout()
    figures.append(("udp_rtt_loss.png", fig))

    fig, ax = plt.subplots(figsize=(10, 5))
    plotted = False
    for label in labels:
        label_rows = [row for row in rows if row["_label"] == label and row.get("status") == "ok" and row["rtt_ms"] is not None]
        label_rows.sort(key=lambda row: (row["sample_index"] or 0, row["seq"] or 0))
        if not label_rows:
            continue
        ax.plot(
            [row["sample_index"] for row in label_rows],
            [row["rtt_ms"] for row in label_rows],
            marker="o",
            linewidth=1.5,
            label=label,
        )
        plotted = True
    if plotted:
        ax.set_title("UDP RTT Trace")
        ax.set_xlabel("Probe index")
        ax.set_ylabel("Milliseconds")
        ax.legend()
        fig.tight_layout()
        figures.append(("udp_rtt_trace.png", fig))
    else:
        plt.close(fig)

    if out_dir:
        output_dir = Path(out_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename, figure in figures:
            figure.savefig(output_dir / filename, dpi=180)

    return figures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plot direct UDP RTT CSV captures")
    parser.add_argument("csv_files", nargs="+", help="one or more CSV captures from udp_rtt.py")
    parser.add_argument("--out-dir", default="udp_rtt_plots", help="output directory for PNG plots")
    args = parser.parse_args(argv)

    rows = load_rtt_rows(args.csv_files)
    if not rows:
        print("No rows found in the supplied CSV files.")
        return 1

    try:
        plot_rtt_rows(rows, args.out_dir)
    except ImportError:
        print("matplotlib is required to generate plots")
        return 1

    print(f"wrote plots to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
