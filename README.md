# ClientSide_PYNQ_jupyter

To spawn into the game environment, and see on HDMI, run:

```bash
python3 launch.py
```

at the root of this directory, with your credentials.

## WiFi Setup

Before running, ensure the PYNQ board is connected to the network. Open [wifi.ipynb](wifi.ipynb) and run each cell — update the `ssid` and `pwd` variables with your hotspot credentials, then confirm the board is pinging before proceeding.

## Server / Monitor

The server and monitor must be running for these notebooks to work. Launch them from the [ServerSide_PYNQ_Raycaster](https://github.com/Akendall47/ServerSide_PYNQ_Raycaster) repo at its root provided you have server access - ie. the *.pem file for an IAM role) :

```bash
./pynq_dev.sh
```

### Running Tests

To run tests, launch the server/monitor from the `test_RTT` branch of the ServerSide repo instead - on the pynq board, cd into `pynq_client_tests` and run notebooks from there.

## Swapping the Bitstream (.bit / .hwh)

Two bitstreams are included:

| Files | Description |
|---|---|
| `design_1_wrapper.bit` / `design_1_wrapper.hwh` | Standard build |
| `design_1_wrapper_neon.bit` / `design_1_wrapper_neon.hwh` | Neon variant |

To swap the active bitstream, update the `OVERLAY_PATH` constant near the top of [run_pynq.py](run_pynq.py):

```python
OVERLAY_PATH = "/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit"
```

Change `design_1_wrapper` to `design_1_wrapper_neon` (or vice versa). The `.hwh` file must always match the `.bit` file — copy both to the board together.

## Permissions / .pem File

The server requires correct permissions and a `.pem` file to run. This file cannot be shared on GitHub — it may have been provided separately to the assessor.
