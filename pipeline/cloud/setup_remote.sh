#!/usr/bin/env bash
# Run ON the Lambda instance (cloud/start_stage.sh setup, or directly): system tools, a venv that
# reuses Lambda Stack's CUDA torch (--system-site-packages), the pipeline's Python deps, and a GPU check.
set -euxo pipefail
cd ~/hh/pipeline
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y -q
sudo apt-get install -y -q osmium-tool tmux pigz python3-venv
osmium --version | head -1
if [ ! -x ~/hhvenv/bin/python ]; then
  python3 -m venv --system-site-packages ~/hhvenv
fi
~/hhvenv/bin/python -m pip install -q --upgrade pip
~/hhvenv/bin/python -m pip install -q -r cloud/requirements-cloud.txt
~/hhvenv/bin/python - <<'EOF'
import numpy, torch, torchvision, rasterio, shapely, pyproj, pyarrow, ultralytics
print("python ok; numpy", numpy.__version__, "torch", torch.__version__, "torchvision", torchvision.__version__)
print("rasterio", rasterio.__version__, "gdal", rasterio.__gdal_version__, "shapely", shapely.__version__,
      "pyarrow", pyarrow.__version__, "ultralytics", ultralytics.__version__)
assert torch.cuda.is_available(), "CUDA not available to torch"
print("cuda", torch.version.cuda, torch.cuda.get_device_name(0))
x = torch.from_numpy(numpy.ones((2, 2), dtype="float32")).cuda() * 2  # numpy <-> torch ABI check
print("torch/numpy interop ok", float(x.sum()))
EOF
nproc; free -g | head -2; df -h / | tail -1; nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
