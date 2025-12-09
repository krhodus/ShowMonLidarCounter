# TFmini Car Counter

Tracks cars with a TFmini-Plus LiDAR on a Raspberry Pi. Features:

- Rising-edge detection
- SQLite logging
- MQTT publishing
- Flask web UI
- Configurable detection window
- Reset counter

## Install

Clone repo and run:

```bash
git clone https://github.com/YOUR_USERNAME/tfmini-car-counter.git
cd tfmini-car-counter
chmod +x install.sh
./install.sh