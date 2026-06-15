#!/bin/bash
# One-shot setup for Media Intel Dashboard.
# Run as: sudo bash ~/mediaintel/setup.sh

set -e

echo "=== Media Intel Setup ==="

# 1. Install Flask
echo "[1/5] Installing python3-flask..."
apt-get install -y python3-flask

# 2. Create media directories
echo "[2/5] Creating media directories..."
mkdir -p /srv/media/review /srv/media/inbox /srv/media/specials
chown vinnie:vinnie /srv/media/review /srv/media/inbox /srv/media/specials

# 3. Install scripts to /usr/local/bin
echo "[3/5] Installing scripts..."
cp /home/vinnie/mediaintel/bin/riptv    /usr/local/bin/riptv
cp /home/vinnie/mediaintel/bin/encodetv /usr/local/bin/encodetv
chmod +x /usr/local/bin/riptv /usr/local/bin/encodetv

# 4. Initialise database
echo "[4/5] Initialising database..."
cd /home/vinnie/mediaintel
python3 -c "import db; db.init_db(); print('  DB ready at', str(db._db_path()))"

# 5. Enable systemd service
echo "[5/5] Enabling systemd service..."
cp /home/vinnie/mediaintel/mediaintel.service /etc/systemd/system/mediaintel.service
systemctl daemon-reload
systemctl enable mediaintel
systemctl restart mediaintel

echo ""
echo "=== Setup complete ==="
echo ""
echo "Dashboard:  http://dexterslab:8088/media"
echo "Status:     systemctl status mediaintel"
echo "Logs:       journalctl -u mediaintel -f"
echo ""
echo "To add your Anthropic API key:"
echo "  sudo systemctl edit mediaintel"
echo '  Add: Environment=ANTHROPIC_API_KEY=sk-ant-...'
echo ""
