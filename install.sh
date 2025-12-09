#!/bin/bash
set -e

echo "Installing TFmini Car Counter..."

sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# Create virtual environment
python3 -m venv ~/tfmini-venv
source ~/tfmini-venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Create systemd service
sudo cp systemd/LidarCounter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable LidarCounter
sudo systemctl start LidarCounter

echo "Install complete. Access the web UI at http://<pi-ip>:5000"