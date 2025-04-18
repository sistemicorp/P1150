from setuptools import Extension, setup

setup(
    name="cobs",
    version="0.1.0",
    description="COBS encoder/decoder",
    install_requires=[],
    ext_modules=[
        Extension(
            name="cobs",
            sources=["cobs.c"],
        ),
    ]
)
