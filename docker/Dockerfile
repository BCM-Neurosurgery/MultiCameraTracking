# syntax=docker/dockerfile:1
FROM nvidia/cuda:12.2.2-runtime-ubuntu22.04

ARG HOST_UID=1000
ARG HOST_GID=1000

# Create non-root user with matching host UID/GID
RUN groupadd -g ${HOST_GID} appuser \
 && useradd -m -u ${HOST_UID} -g appuser -s /bin/bash appuser

ENV HOME=/home/appuser \
    NPM_CONFIG_CACHE=/home/appuser/.npm \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

ARG DEBIAN_FRONTEND=noninteractive

### Core system dependencies
RUN apt-get update && apt-get install -y \
    python3 python3-dev python3-pip \
    libusb-1.0-0 libgl1 libglib2.0-0 ffmpeg \
    curl screen net-tools git \
    ca-certificates gnupg \
    libtbb-dev \
    && rm -rf /var/lib/apt/lists/*

### FFmpeg with NVENC support (BtbN static build — includes libx264, h264_nvenc, hevc_nvenc)
### This replaces the old volume-mount hack with a reproducible build step
RUN curl -fsSL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz \
    | tar -xJ --strip-components=2 -C /usr/local/bin/ \
      ffmpeg-master-latest-linux64-gpl/bin/ffmpeg \
      ffmpeg-master-latest-linux64-gpl/bin/ffprobe

### Install Spinnaker SDK 4.3 (Ubuntu 22.04, Python 3.10)
COPY docker/flir/spinnaker-4.3.0.189-amd64 /tmp/spinnaker-sdk
COPY docker/flir/spinnaker_python-4.3.0.189-cp310-cp310-linux_x86_64.whl /tmp/

# Install only the runtime library debs (skip spinview-qt, -dev, -doc to avoid Qt deps)
RUN echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections && \
    echo libgentl libspinnaker/accepted-flir-eula boolean true | debconf-set-selections && \
    dpkg -i /tmp/spinnaker-sdk/libgentl_*.deb \
            /tmp/spinnaker-sdk/libspinnaker_4.3.0.189_amd64.deb \
            /tmp/spinnaker-sdk/libspinnaker-c_*.deb \
            /tmp/spinnaker-sdk/libspinvideo_*.deb \
            /tmp/spinnaker-sdk/libspinvideo-c_*.deb \
            /tmp/spinnaker-sdk/spinupdate_*.deb \
            /tmp/spinnaker-sdk/spinnaker_4.3.0.189_amd64.deb || true && \
    apt-get update && apt-get -f install -y && \
    pip3 install /tmp/spinnaker_python-4.3.0.189-cp310-cp310-linux_x86_64.whl && \
    rm -rf /tmp/spinnaker-sdk /tmp/*.whl /var/lib/apt/lists/*

### Install Node.js 20.x
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
    | tee /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p ${NPM_CONFIG_CACHE} \
 && chown -R appuser:appuser ${NPM_CONFIG_CACHE}

### Build React frontend
WORKDIR /Mocap/react_frontend
COPY react_frontend/package*.json ./
RUN npm install
COPY react_frontend/ ./

### Install EasyMocap (for future 3D reconstruction pipeline)
WORKDIR /
RUN git clone https://github.com/peabody124/EasyMocap && \
    pip3 install --upgrade pip && \
    pip3 install -e /EasyMocap

### Performance libraries
RUN pip3 install tbb numba

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

USER appuser

ENTRYPOINT ["/bin/bash"]
CMD ["/Mocap/start_acquisition_gui.sh"]
