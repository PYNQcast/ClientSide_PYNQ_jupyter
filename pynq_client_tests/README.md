# PYNQ Client Tests

Board-side latency benchmarking. Self-contained — copy the whole folder to the board:

```bash
scp -r jupyter_side/pynq_client_tests xilinx@<PYNQ_IP>:/home/xilinx/jupyter_notebooks/Final_project_test/
```

## Quickstart

Use the notebook **`UDP_RTT_Benchmark.ipynb`** — set params in cell 2, run all cells.

Or from the terminal inside this folder (use `run_pynq_rtt.py` directly):

```bash
# plain RTT
python3 run_pynq_rtt.py --server 3.9.71.204 --samples 100 --label idle \
  --csv-out data/udp_rtt_idle.csv --json-out data/udp_rtt_idle.json

# button-to-visible (press a board button for each sample)
python3 run_pynq_rtt.py --server 3.9.71.204 --samples 20 --label b2vis \
  --measure button_to_visible \
  --csv-out data/button_to_visible.csv --json-out data/button_to_visible.json
```

## What is measured

**`rtt`** — UDP echo probe: board → EC2 server → board. Raw network path, no game logic.

**`button_to_visible`** — full path: new button press edge → `PKT_STATE_UPDATE` sent → `PKT_GAME_STATE` reply received → pose written to BRAM. One packet sent per sample (press edge, not hold), so the player moves one step on the monitor canvas per press.

Does not measure monitor/browser rendering delay.

## Key stats

| stat | meaning |
|---|---|
| `p95_rtt_ms` | network stability headline |
| `avg_rtt_ms` / `max_rtt_ms` | typical / worst-case RTT |
| `button_to_visible_p95_ms` | worst-case button-to-BRAM latency |
| `loss_pct` | % of probes that timed out |

## Plotting saved CSVs

```python
from pynq_client_tests.plot_udp_rtt_csv import load_rtt_rows, plot_rtt_rows, summarise_by_label

rows = load_rtt_rows(["data/udp_rtt_idle.csv", "data/udp_rtt_dual-board.csv"])
summarise_by_label(rows)
plot_rtt_rows(rows)
# writes: udp_rtt_comparison.png, udp_rtt_loss.png, udp_rtt_trace.png
```
