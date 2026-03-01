# MnemoPane Phase 1 Bring-Up

## 1. Clone Repo

git clone <repo>
cd MnemoPane

## 2. Install Python deps (admin)
python3 -m venv mnemopane-venv
source mnemopane-venv/bin/activate
pip install -r admin/requirements.txt

## 3. Install systemd services
sudo scripts/install_services.sh

## 4. Verify
scripts/status.sh