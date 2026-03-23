# ClientSide_PYNQ_jupyter

![Python 3.12](https://img.shields.io/badge/Python-3.12-blue)
![Board](https://img.shields.io/badge/Board-PYNQ--Z1-green)
![Server](https://img.shields.io/badge/Server-EC2_asyncio-orange)
![License](https://img.shields.io/badge/License-MIT-yellow)

Client-side code for the PYNQ-Z1 raycaster. Runs on the board, renders to HDMI, and connects to an EC2 game server for multiplayer logic.

To spawn into the game environment and see output on HDMI, run:

```bash
python3 launch.py
```

> [!IMPORTANT]
> Complete WiFi setup and ensure the board is network-reachable before running `launch.py`.

---

## WiFi Setup

Open [wifi.ipynb](wifi.ipynb) and run each cell. Update the credentials at the top:

```python
ssid = "YOUR_HOTSPOT"
pwd  = "YOUR_PASSWORD"
```
---

## Server / Monitor

The server and monitor must be running to get the full game experience. However, `run_pynq.py` and `launch.py` are configured so that the bitstream will run without the game logic — you will still be able to raycast and move around.

Launch the server from the [ServerSide_PYNQ_Raycaster](https://github.com/Akendall47/ServerSide_PYNQ_Raycaster) repo at its root, provided you have server access (i.e. the `.pem` file for an IAM role):

```bash
./pynq_dev.sh
```

### Running Server Tests

To run tests, launch the server/monitor from the `test_RTT` branch of the ServerSide repo instead. On the PYNQ board, `cd` into `pynq_client_tests` and run notebooks from there.

---

## Swapping the Bitstream (.bit / .hwh)

Two bitstreams are included — **neon is the most up-to-date**, with improved textures and physics:

| Files | Description |
|---|---|
| `design_1_wrapper.bit` / `design_1_wrapper.hwh` | Standard basic build |
| `design_1_wrapper_neon.bit` / `design_1_wrapper_neon.hwh` | Neon variant (recommended) |

To swap the active bitstream, update `OVERLAY_PATH` near the top of [run_pynq.py](run_pynq.py):

```python
OVERLAY_PATH = "/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit"
```

Change `design_1_wrapper` to `design_1_wrapper_neon` (or vice versa).

> [!IMPORTANT]
> The `.hwh` file must always match the `.bit` file — copy both to the board together.

---

## Permissions / .pem File

> [!NOTE]
> The `.pem` file required for server access cannot be shared on GitHub — it may have been provided separately to the assessor.
