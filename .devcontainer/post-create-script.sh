sudo apt update
sudo apt upgrade -y

python -m pip install --upgrade pip
python -m venv .venv

./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pip install -r pyyaml