# Pass a pinned SWE-bench base image; no source changes or runtime downloads.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN /opt/miniconda3/envs/testbed/bin/python -m pip install --no-cache-dir \
    pytest==7.4.4 iniconfig==2.0.0 packaging==24.2 pluggy==1.5.0 \
    exceptiongroup==1.2.2 tomli==2.2.1
