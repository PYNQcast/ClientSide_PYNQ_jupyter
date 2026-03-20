# pynq_jupyter

to to spawn into the game environment, and see on HDMI, run: 

```bash
python3 launch.py
```

at the root of this directory, with your credentials. 

## Server / Monitor

The server and monitor must be running for these notebooks to work. Launch them from the [ServerSide_PYNQ_Raycaster](https://github.com/Akendall47/ServerSide_PYNQ_Raycaster) repo at its root:

```bash
./pynq_dev.sh
```

### Running Tests

To run tests, launch the server/monitor from the `test_RTT` branch of the ServerSide repo instead.

## Permissions / .pem File

The server requires correct permissions and a `.pem` file to run. This file cannot be shared on GitHub — it may have been provided separately to the assessor.
