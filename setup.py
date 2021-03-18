from setuptools import setup, find_packages

setup(
    name="asm_kernel",
    version="0.1",
    python_requires=">=3.6",
    packages=find_packages(),
    install_requires=["jupyter_client", "IPython", "ipykernel"],
)
