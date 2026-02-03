# syntax=docker/dockerfile:1
FROM peabody124/posepipe

# Allow passing host UID/GID at build time (defaults to 1000)
ARG HOST_UID=1000
ARG HOST_GID=1000

# Create a non-root user 'appuser' with matching UID/GID
RUN groupadd -g ${HOST_GID} appuser \
 && useradd -m -u ${HOST_UID} -g appuser -s /bin/bash appuser

# Set HOME and npm cache directory under the non-root user
ENV HOME=/home/appuser \
    NPM_CONFIG_CACHE=/home/appuser/.npm

### Install Spinnaker
COPY docker/flir/spinnaker-3.1.0.79-amd64 /spinnaker
COPY docker/flir/*.whl /spinnaker

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y libusb-1.0-0 qtbase5-dev qt5-default qtchooser qt5-qmake qtbase5-dev-tools curl screen && \
    echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections && \
    echo libgentl libspinnaker/accepted-flir-eula boolean true | debconf-set-selections && \
    dpkg -i /spinnaker/*.deb && \
    pip3 install /spinnaker/*.whl

### Install Node.js & npm
RUN apt-get install -y ca-certificates curl gnupg && mkdir -p /etc/apt/keyrings
RUN curl -fsSL \
      https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
RUN echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
      | tee /etc/apt/sources.list.d/nodesource.list
RUN apt-get update && apt-get install -y nodejs

# Prepare npm cache directory and adjust ownership
RUN mkdir -p ${NPM_CONFIG_CACHE} \
 && chown -R appuser:appuser ${NPM_CONFIG_CACHE}

### Build React frontend
WORKDIR /Mocap/react_frontend
COPY react_frontend/package*.json ./
RUN npm install
COPY react_frontend/ ./

### Additional utilities
RUN apt-get install -y net-tools

### Install EasyMocap
WORKDIR /
RUN git clone https://github.com/peabody124/EasyMocap && \
    pip install --upgrade pip && \
    pip install -e /EasyMocap

### Biomechanical visualization dependencies
#
# Pin Torch to match the PyTorch3D wheel below (py38_cu117_pyt201).
# This avoids pip pulling newer CUDA 12.x Torch deps (e.g., nvidia_cudnn_cu12) during build.
# RUN pip3 install --no-cache-dir \
#       --index-url https://download.pytorch.org/whl/cu117 \
#       --extra-index-url https://pypi.org/simple \
#       torch==2.0.1+cu117 && \
#     pip3 install fvcore iopath trimesh pyvista && \
#     pip3 install --no-index --no-cache-dir pytorch3d \
#       -f https://dl.fbaipublicfiles.com/pytorch3d/packaging/wheels/py38_cu117_pyt201/download.html

RUN apt-get install -y libtbb-dev && pip3 install tbb numba && pip3 install --upgrade tbb

### MultiCameraTracking
WORKDIR /Mocap
COPY requirements.txt ./
RUN pip3 install -r requirements.txt
COPY pyproject.toml README.md ./
COPY multi_camera ./multi_camera
COPY model_data ./model_data
RUN pip3 install -e .

# Entry scripts
COPY docker/start_acquisition_gui.sh /Mocap/start_acquisition_gui.sh
RUN chmod +x /Mocap/start_acquisition_gui.sh

# Ensure data/config dirs exist
WORKDIR /
RUN mkdir -p /data /configs

# Use the non-root user from here on
USER appuser

ENTRYPOINT ["/bin/bash"]
CMD ["/Mocap/start_acquisition_gui.sh"]
