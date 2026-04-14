sudo apt-get update 
sudo apt-get install -y libgl1 # needed for opencv
pip install -r requirements-gpu.txt
pip install -e .
