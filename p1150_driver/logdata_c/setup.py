from setuptools import setup, Extension

# List of libcbor source files, assuming they are in the current directory.
libcbor_sources = [
    'allocators.c',
    'arrays.c',
    'common.c',
    'bytestrings.c',
    'cbor.c',
    'floats_ctrls.c',
    'builder_callbacks.c',
    'ints.c',
    'maps.c',
    'stack.c',
    'tags.c',
    'memory_utils.c',
    'strings.c',
    'loaders.c',
    'streaming.c',
    'unicode.c',
]

logdata_ext = Extension(
    "logdata_ext",
    # Add the libcbor sources to the main extension source file.
    sources=["logdatamodule.c"] + libcbor_sources,
    # The include_dirs path tells the compiler where to find the libcbor .h files.
    include_dirs=[],
    extra_compile_args=[],
    extra_link_args=[],
)

setup(
    name="logdata_ext",
    version="0.2.0",
    description="C extension for log data processing with bundled libcbor.",
    ext_modules=[logdata_ext],
)
