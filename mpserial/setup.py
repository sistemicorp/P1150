# setup.py
from setuptools import setup, Extension

ext = Extension(
    "mp_serial_ext",
    sources=["mp_serial_ext.c"],
    extra_compile_args=[],
    extra_link_args=[],
)

setup(
    name="mp_serial_ext",
    version="0.1.0",
    ext_modules=[ext],
)
