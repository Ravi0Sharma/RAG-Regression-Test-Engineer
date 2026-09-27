# Test runner dependencies only; preserve the historical SymPy source/environment.
FROM swebench/sweb.eval.x86_64.sympy_1776_sympy-20442@sha256:74f959bc28c3b74c0e9998d1f347342dedd4982faa7600bfda60319896dcf9ad
RUN /opt/miniconda3/envs/testbed/bin/python -m pip install --no-cache-dir \
    pytest==7.4.4 iniconfig==2.0.0 packaging==24.2 pluggy==1.5.0 \
    exceptiongroup==1.2.2 tomli==2.2.1
