# ShowMon Lidar Car Counter

Tracks cars with a TFmini-Plus LiDAR on a Raspberry Pi. Features:

- Rising-edge detection
- SQLite logging
- MQTT publishing
- Flask web UI
- Configurable detection window
- Follows schedule published on private GitHub repo
- Automatic backup to Azure blob


## Install

Clone repo and run:

```bash
git clone https://github.com/krhodus/ShowMon-CarCounter.git
cd ShowMon-CarCounter
chmod +x install.sh
./install.sh