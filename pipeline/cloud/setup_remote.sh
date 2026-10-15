#!/usr/bin/env bash
# Run ON the Lambda instance (cloud/start_stage.sh setup, or directly): the watchdog timer, system tools, a venv
# that reuses Lambda Stack's CUDA torch (--system-site-packages), the pipeline's Python deps, and a GPU check.
set -euxo pipefail
cd ~/hh/pipeline
# Safety first: (re)install the self-terminating watchdog. It needs the config and key that launch.sh /
# install_watchdog.sh put in /etc/hoophunter; without them setup stops, so nothing runs on an unprotected node.
if [ "${HH_NO_WATCHDOG:-0}" != "1" ]; then
  sudo bash cloud/watchdog_install.sh
fi
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y -q
sudo apt-get install -y -q osmium-tool tmux pigz python3-venv
osmium --version | head -1
if [ ! -x ~/hhvenv/bin/python ]; then
  python3 -m venv --system-site-packages ~/hhvenv
fi
~/hhvenv/bin/python -m pip install -q --upgrade pip
~/hhvenv/bin/python -m pip install -q -r cloud/requirements-cloud.txt
# TensorRT 10 for scan_engine --engine. The wheels are installed without dependency resolution: pip >= 24.1 refuses
# Lambda Stack's system flatbuffers version string, and onnx >= 1.18 / ml-dtypes 0.6 pull numpy 2, which breaks the
# system torch/scipy (built for numpy 1.x). HH_TRT=0 skips this; HH_TRT_EXPORT=1 also installs what an engine
# *build* (Ultralytics ONNX export) needs.
if [ "${HH_TRT:-1}" = "1" ]; then
  ~/hhvenv/bin/python -m pip install -q --no-deps "tensorrt-cu12<11" "tensorrt-cu12-libs<11" "tensorrt-cu12-bindings<11" || true
  if [ "${HH_TRT_EXPORT:-0}" = "1" ]; then
    ~/hhvenv/bin/python -m pip install -q "onnx<1.18" "onnxslim<0.1.60" "ml-dtypes<0.6" || true
  fi
  ~/hhvenv/bin/python -m pip install -q "numpy==1.26.4"
fi
~/hhvenv/bin/python - <<'EOF'
import numpy, torch, torchvision, rasterio, shapely, pyproj, pyarrow, ultralytics
assert numpy.__version__.startswith("1."), f"numpy {numpy.__version__}: Lambda Stack torch/scipy need numpy 1.x"
print("python ok; numpy", numpy.__version__, "torch", torch.__version__, "torchvision", torchvision.__version__)
try:
    import tensorrt
    print("tensorrt", tensorrt.__version__)
except ImportError:
    print("tensorrt not installed (scan_engine runs PyTorch FP16)")
print("rasterio", rasterio.__version__, "gdal", rasterio.__gdal_version__, "shapely", shapely.__version__,
      "pyarrow", pyarrow.__version__, "ultralytics", ultralytics.__version__)
assert torch.cuda.is_available(), "CUDA not available to torch"
print("cuda", torch.version.cuda, torch.cuda.get_device_name(0))
x = torch.from_numpy(numpy.ones((2, 2), dtype="float32")).cuda() * 2  # numpy <-> torch ABI check
print("torch/numpy interop ok", float(x.sum()))
EOF
nproc; free -g | head -2; df -h / | tail -1; nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
