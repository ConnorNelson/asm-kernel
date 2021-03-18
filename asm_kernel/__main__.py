from ipykernel.kernelapp import IPKernelApp
from . import ASMKernel

IPKernelApp.launch_instance(kernel_class=ASMKernel)
