# Historical pvlib imports np.Inf, removed by NumPy 2. No source changes.
FROM swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1154@sha256:e2f3b1ebb69820fe1195facf4a897db1357048ba5fe23a294def3ba617a2ad20
RUN /opt/miniconda3/envs/testbed/bin/python -m pip install --no-cache-dir numpy==1.26.4
